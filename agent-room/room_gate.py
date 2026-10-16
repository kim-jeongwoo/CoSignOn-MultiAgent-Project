"""대화방 입구: 127.0.0.1:8095 — 관리자 승인 방식 로그인 + 대화방 페이지(/room) + 관리자 화면(/admin) + 접속 기록.

- 승인 요청(/__room/request): 쿠키가 없는 사람은 이름을 넣고 요청한다. 화면에 6자리 확인 코드가 뜨고,
  그 코드를 관리자에게 카톡으로 알린다. 요청 화면은 3초마다 승인 여부를 확인하다가 승인되면 /room으로 간다.
  요청에는 Cloudflare가 넘겨주는 방문자 위치 헤더(국가·도시)와 IP, 기기·브라우저가 함께 기록된다(외부 위치 조회 없음).
- 승인: 그 브라우저에 30일 쿠키(서버 비밀키 HMAC 서명, HttpOnly·Secure·SameSite=Lax). 서버의 승인 목록에도 남겨서
  관리자가 승인 취소하면 즉시 막힌다. 30일이 지나면 다시 요청.
- 관리자(/admin): 아이디 + 비밀번호(bcrypt 해시만 저장). 같은 IP 실패 5번이면 30분 차단, 전체 실패가 1시간에
  20번을 넘으면 관리자 로그인 1시간 잠금. 관리자 세션 쿠키 7일.
- 권한: 일반 사용자는 대화방(/room)과 대화에 필요한 Studio API만, Studio 화면(팀 편집)은 관리자만 (nginx auth_request → /auth).
- 스팸 방지: 같은 IP의 승인 요청은 10분에 3번까지, 처리 안 된 요청은 1시간 뒤 자동 만료.
- 접속 기록(logs/access_log.jsonl): 승인 요청·승인·거절·승인 취소·요청 만료·입장·나가기·자동 퇴장·관리자 로그인 성공/실패/차단.
- 관리자 비밀번호 바꾸기: .venv/bin/python room_gate.py set-admin <아이디>  (비밀번호는 화면에 표시되지 않게 입력)
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

ROOM = Path.home() / "agent-room"
STATE = ROOM / "gate_state"
APPROVALS_FILE = STATE / "approvals.json"
ADMIN_FILE = STATE / "admin.json"
SECRET_FILE = ROOM / ".gate_secret"
ACCESS_LOG = ROOM / "logs" / "access_log.jsonl"
CHAT_HTML = ROOM / "room_chat.html"
ADMIN_NAME = "김정우"

USER_DAYS, ADMIN_DAYS = 30, 7
REQ_TTL = 3600                       # 처리 안 된 요청 자동 만료(초)
REQ_LIMIT, REQ_WINDOW = 3, 600       # 같은 IP 승인 요청: 10분에 3번
IP_FAILS, IP_BLOCK = 5, 1800         # 관리자 로그인: 같은 IP 5번 실패 → 30분 차단
GLOBAL_FAILS, GLOBAL_WINDOW, GLOBAL_LOCK = 20, 3600, 3600  # 전체 실패 1시간 20번 초과 → 1시간 잠금
IDLE_SEC, ACTIVE_SEC = 180, 30       # 3분 신호 없으면 자동 퇴장, 30초 안 신호면 '접속 중'

STATE.mkdir(mode=0o700, exist_ok=True)
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32))
    SECRET_FILE.chmod(0o600)
SECRET = SECRET_FILE.read_text().strip().encode()

app = FastAPI()
requests_: dict[str, dict] = {}      # 승인 요청 id → 요청 정보(메모리, 1시간 만료)
req_times: dict[str, list] = {}      # IP → 승인 요청 시각들
ip_fails: dict[str, list] = {}       # IP → 관리자 로그인 실패 시각들
ip_block: dict[str, float] = {}      # IP → 차단 해제 시각
global_fails: list = []
global_lock = [0.0]
presence: dict[str, dict] = {}       # 접속 키 → {name, last, ip, role}


# ---------- 저장·서명·기록 ----------
def _write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def load_approvals() -> dict:
    return json.loads(APPROVALS_FILE.read_text(encoding="utf-8")) if APPROVALS_FILE.exists() else {}


approvals = load_approvals()


def save_approvals():
    _write_json(APPROVALS_FILE, approvals)


def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return body + "." + hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()


def unsign(token: str | None):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    return data if data.get("exp", 0) > time.time() else None


def log_access(name: str, event: str, ip: str = "", **extra):
    ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": datetime.now().isoformat(timespec="seconds"), "name": name, "event": event, "ip": ip, **extra}
    with ACCESS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------- 방문자 정보 (Cloudflare 헤더만 사용) ----------
COUNTRY = {"KR": "대한민국", "US": "미국", "JP": "일본", "CN": "중국", "TW": "대만", "HK": "홍콩", "SG": "싱가포르",
           "VN": "베트남", "DE": "독일", "GB": "영국", "FR": "프랑스", "CA": "캐나다", "AU": "호주", "XX": "알 수 없음",
           "T1": "Tor"}


def visitor(req: Request) -> dict:
    h = req.headers
    cc = h.get("cf-ipcountry", "")
    ua = h.get("user-agent", "")
    return {"ip": h.get("cf-connecting-ip") or (req.client.host if req.client else ""),
            "country": (COUNTRY.get(cc, cc) + (f" ({cc})" if cc and cc in COUNTRY and cc not in ("XX", "T1") else "")) if cc else "",
            "region": h.get("cf-region", ""), "city": h.get("cf-ipcity", ""), "device": parse_ua(ua), "ua": ua[:300]}


def parse_ua(ua: str) -> str:
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else
           "Mac" if "Mac OS X" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "")
    br = ("카카오톡 브라우저" if "KAKAOTALK" in ua else "네이버 웨일" if "Whale/" in ua else "삼성 인터넷" if "SamsungBrowser" in ua else
          "Edge" if "Edg/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else
          "Safari" if "Safari/" in ua else "")
    kind = "모바일" if ("Mobile" in ua or "Android" in ua) else "PC" if os_ else ""
    return " · ".join(x for x in (kind, os_, br) if x) or (ua[:40] or "알 수 없음")


# ---------- 누구인가 ----------
def admin_of(req: Request):
    d = unsign(req.cookies.get("room_admin"))
    if d and d.get("role") == "admin":
        acc = admin_account()
        if acc and d.get("id") == acc["id"] and d.get("v") == acc.get("v"):
            return d
    return None


def user_of(req: Request):
    d = unsign(req.cookies.get("room_auth"))
    if not d:
        return None
    a = approvals.get(d.get("aid", ""))
    if not a or a.get("revoked_at") or a["expires_at"] < time.time():
        return None
    return a


def who(req: Request):
    """(역할, 표시 이름, 접속 키) 또는 None"""
    ad = admin_of(req)
    if ad:
        return "admin", f"{ADMIN_NAME}(관리자)", "admin:" + ad["sid"]
    u = user_of(req)
    if u:
        return "user", u["name"], "user:" + u["aid"]
    return None


def sweep():
    now = time.time()
    for k, p in list(presence.items()):
        if now - p["last"] > IDLE_SEC:
            presence.pop(k, None)
            log_access(p["name"], "자동 퇴장(화면 닫힘)", p["ip"])
    for rid, r in list(requests_.items()):
        if r["status"] == "pending" and now - r["created"] > REQ_TTL:
            r["status"] = "expired"
            log_access(r["name"], "요청 만료(1시간)", r["ip"])
        if now - r["created"] > REQ_TTL * 3:
            requests_.pop(rid, None)


def touch(req: Request, w):
    role, name, key = w
    p = presence.get(key)
    if not p:
        log_access(name, "입장", visitor(req)["ip"])
        presence[key] = p = {"name": name, "ip": visitor(req)["ip"], "role": role}
    p["last"] = time.time()


# ---------- nginx auth_request: Studio 쪽 요청 권한 ----------
USER_API = [("GET", re.compile(r"^/api/teams/(\?.*)?$")), ("POST", re.compile(r"^/api/sessions/$")),
            ("POST", re.compile(r"^/api/runs/$")), ("GET", re.compile(r"^/api/ws/runs/\d+$"))]


@app.get("/auth")
async def auth(req: Request):
    sweep()
    w = who(req)
    if not w:
        return Response(status_code=401)
    if w[0] == "admin":
        return Response(status_code=200)
    uri, method = req.headers.get("x-original-uri", ""), req.headers.get("x-original-method", "GET")
    if any(m == method and p.match(uri) for m, p in USER_API):
        return Response(status_code=200)
    return Response(status_code=403)   # Studio 화면·팀 편집은 관리자만


# ---------- 승인 요청 ----------
BASE_CSS = """
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d1f23;--muted:#6b7280;--line:#d6d9de;--accent:#2563eb;--err:#c0392b;--ok:#19692c}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#20232a;--fg:#e8eaed;--muted:#9aa0a6;--line:#3a3f47;--accent:#5b8def;--err:#ff7b6b;--ok:#6fd38a}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;padding:16px}
.card{width:100%;max-width:380px;margin:8vh auto 0;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 22px}
h1{font-size:20px;margin:0 0 4px}.sub{margin:0 0 18px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font-size:15px}
input:focus{outline:2px solid var(--accent);border-color:transparent}
button{font:inherit;border:0;border-radius:8px;padding:10px 14px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
button.full{width:100%;margin-top:18px}button:disabled{opacity:.5;cursor:default}
.note{font-size:12px;color:var(--muted);margin-top:14px;line-height:1.6}.err{color:var(--err);font-size:13px;margin-top:10px}
.code{font:700 34px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:6px;text-align:center;padding:14px;border:1px dashed var(--line);border-radius:10px;margin:14px 0}
"""

REQUEST_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-room 입장 요청</title><style>""" + BASE_CSS + """</style></head><body><div class="card">
<h1>agent-room</h1><p class="sub">클로드 · 제미나이 · 딥시크 대화방 — 관리자 승인 후 입장</p>
<div id="form"><label for="n">이름</label><input id="n" maxlength="20" placeholder="예: 김정우" autocomplete="off" autofocus>
<button class="full" id="go">입장 요청</button><div class="err" id="err"></div></div>
<div id="wait" hidden><div>확인 코드</div><div class="code" id="code"></div>
<div><b>이 코드를 __ADMIN__에게 카톡으로 알려 주세요.</b></div><div class="note" id="st">승인을 기다리는 중…</div></div>
<p class="note">요청 시 IP 기준 대략적 위치와 기기 정보가 관리자에게 전달됩니다.</p>
</div><script>
(function(){
  var $=function(i){return document.getElementById(i)}, next=new URLSearchParams(location.search).get('next')||'/room', timer=null;
  if(!/^\\/(?!\\/)/.test(next)) next='/room';
  function waiting(code){ $('form').hidden=true; $('wait').hidden=false; $('code').textContent=code; poll(); timer=setInterval(poll,3000); }
  function poll(){ fetch('/__room/request/status',{cache:'no-store',credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
    if(d.status==='approved'){ clearInterval(timer); $('st').textContent='승인되었습니다. 들어가는 중…'; location.href=next; }
    else if(d.status==='rejected'||d.status==='expired'||d.status==='none'){ clearInterval(timer); $('wait').hidden=true; $('form').hidden=false;
      $('err').textContent = d.status==='rejected' ? '요청이 거절되었습니다.' : d.status==='expired' ? '1시간 안에 승인되지 않아 요청이 만료되었습니다. 다시 요청해 주세요.' : ''; }
  }).catch(function(){}); }
  $('go').onclick=function(){ var n=$('n').value.trim(); if(!n){$('err').textContent='이름을 넣어 주세요.';return;}
    $('go').disabled=true; $('err').textContent='';
    fetch('/__room/request',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Room-Request':'1'},body:JSON.stringify({name:n})})
      .then(function(r){return r.json().then(function(d){return [r.status,d]})}).then(function(x){ $('go').disabled=false;
        if(x[0]===200) waiting(x[1].code); else $('err').textContent=x[1].error||'요청하지 못했습니다.'; })
      .catch(function(){ $('go').disabled=false; $('err').textContent='요청하지 못했습니다.'; }); };
  $('n').addEventListener('keydown',function(e){ if(e.key==='Enter'&&!e.isComposing) $('go').click(); });
  fetch('/__room/request/status',{cache:'no-store',credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
    if(d.status==='pending') waiting(d.code); else if(d.status==='approved') location.href=next; });
})();
</script></body></html>""".replace("__ADMIN__", ADMIN_NAME)


def _cookie(resp, name, value, days):
    resp.set_cookie(name, value, max_age=days * 86400, expires=days * 86400, httponly=True, secure=True, samesite="lax", path="/")


@app.get("/__room/request")
async def request_page(req: Request, next: str = "/room"):
    if who(req):
        return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/room", status_code=303)
    return HTMLResponse(REQUEST_PAGE, headers={"Cache-Control": "no-store"})


@app.post("/__room/request")
async def request_create(req: Request):
    sweep()
    if req.headers.get("x-room-request") != "1":
        return JSONResponse({"error": "잘못된 요청"}, status_code=400)
    try:
        name = " ".join(str((await req.json()).get("name", "")).split())[:20]
    except Exception:
        name = ""
    if not name:
        return JSONResponse({"error": "이름을 넣어 주세요."}, status_code=400)
    v = visitor(req)
    now = time.time()
    times = [t for t in req_times.get(v["ip"], []) if now - t < REQ_WINDOW]
    if len(times) >= REQ_LIMIT:
        return JSONResponse({"error": f"요청이 너무 많습니다. {int((REQ_WINDOW - (now - times[0])) // 60) + 1}분 뒤에 다시 시도해 주세요."}, status_code=429)
    req_times[v["ip"]] = times + [now]
    rid = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    requests_[rid] = {"rid": rid, "code": code, "name": name, "created": now, "status": "pending", **v}
    log_access(name, "승인 요청", v["ip"], code=code, country=v["country"], city=v["city"], device=v["device"])
    resp = JSONResponse({"code": code})
    _cookie(resp, "room_req", sign({"rid": rid, "exp": now + REQ_TTL * 3}), 1)
    return resp


@app.get("/__room/request/status")
async def request_status(req: Request):
    sweep()
    d = unsign(req.cookies.get("room_req"))
    r = requests_.get(d["rid"]) if d else None
    if not r:
        return JSONResponse({"status": "approved" if who(req) else "none"}, headers={"Cache-Control": "no-store"})
    resp = JSONResponse({"status": r["status"], "code": r["code"] if r["status"] == "pending" else None},
                        headers={"Cache-Control": "no-store"})
    if r["status"] == "approved":
        a = approvals.get(r.get("aid", ""))
        if a and not a.get("revoked_at"):
            _cookie(resp, "room_auth", sign({"aid": a["aid"], "exp": a["expires_at"]}), USER_DAYS)
        resp.delete_cookie("room_req", path="/")
        requests_.pop(r["rid"], None)
    return resp


# ---------- 대화방·접속자 ----------
def _login_redirect(path="/room"):
    return RedirectResponse("/__room/request?next=" + urllib.parse.quote(path), status_code=303)


@app.get("/room")
async def room(req: Request):
    sweep()
    w = who(req)
    if not w:
        return _login_redirect("/room")
    touch(req, w)
    return HTMLResponse(CHAT_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


USER_EVENTS = {"입장", "나가기", "자동 퇴장(화면 닫힘)", "승인"}


def recent_log(n=30, events=None):
    if not ACCESS_LOG.exists():
        return []
    out = []
    for l in reversed(ACCESS_LOG.read_text(encoding="utf-8").splitlines()[-400:]):
        try:
            d = json.loads(l)
        except Exception:
            continue
        if events is None or d.get("event") in events:
            out.append(d)
        if len(out) >= n:
            break
    return out


@app.get("/__room/presence")
async def presence_api(req: Request):
    sweep()
    w = who(req)
    if not w:
        return JSONResponse({"error": "login"}, status_code=401)
    touch(req, w)
    now = time.time()
    names = sorted({p["name"] for p in presence.values() if now - p["last"] <= ACTIVE_SEC}, key=lambda n: (n != w[1], n))
    log = [{"t": d["t"], "name": d["name"], "event": d["event"]} for d in recent_log(30, USER_EVENTS)]
    return JSONResponse({"me": w[1], "role": w[0], "users": names, "log": log}, headers={"Cache-Control": "no-store"})


@app.get("/__room/logout")
async def logout(req: Request):
    w = who(req)
    if w and presence.pop(w[2], None):
        log_access(w[1], "나가기", visitor(req)["ip"])
    return HTMLResponse("""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>나갔습니다</title><style>""" + BASE_CSS + """</style></head><body><div class="card"><h1>나갔습니다</h1>
<p class="sub">승인 기간 안에는 다시 승인 없이 들어올 수 있습니다.</p><a href="/room"><button class="full">다시 들어가기</button></a></div></body></html>""")


# ---------- 관리자 ----------
def admin_account():
    return json.loads(ADMIN_FILE.read_text(encoding="utf-8")) if ADMIN_FILE.exists() else None


def set_admin(admin_id: str, password: str):
    _write_json(ADMIN_FILE, {"id": admin_id, "v": secrets.token_hex(4),  # v가 바뀌면 기존 관리자 세션은 무효
                             "bcrypt": bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()})


ADMIN_LOGIN = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-room 관리자</title><style>""" + BASE_CSS + """</style></head><body>
<form class="card" method="post" action="/admin/login"><h1>관리자 로그인</h1><p class="sub">agent-room 승인 관리</p>
<label for="i">아이디</label><input id="i" name="id" autocomplete="username" required autofocus>
<label for="p">비밀번호</label><input id="p" name="password" type="password" autocomplete="current-password" required>
__ERR__<button class="full" type="submit">로그인</button></form></body></html>"""

ADMIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-room 관리자</title><style>""" + BASE_CSS + """
.wrap{max-width:1100px;margin:0 auto}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:14px}
header h1{margin:0}header .grow{flex:1}header a{color:var(--accent);text-decoration:none;font-size:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
h2{font-size:15px;margin:0 0 10px}.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top;white-space:nowrap}th{color:var(--muted);font-weight:600;border-top:0}
td.code{font:700 15px ui-monospace,Menlo,monospace;letter-spacing:2px;margin:0;padding:7px 8px;border:0;border-top:1px solid var(--line);text-align:left}
.loc small{color:var(--muted);display:block}.ok{background:var(--ok)}.no{background:var(--err)}td button{padding:5px 10px;font-size:13px;margin-right:4px}
.empty{color:var(--muted);font-size:13px}.ev{font-size:12px;color:var(--muted)}.ev b{color:var(--fg)}
</style></head><body><div class="wrap">
<header><h1>agent-room 관리자</h1><span class="grow"></span><a href="/room">대화방</a><a href="/">Studio(팀 편집)</a>
<form method="post" action="/admin/logout" style="margin:0"><button style="padding:6px 10px;font-size:13px">관리자 로그아웃</button></form></header>
<section><h2>대기 중 요청 <span id="pc"></span></h2><div class="tbl" id="pending"></div></section>
<section><h2>승인된 사람 <span id="ac"></span></h2><div class="tbl" id="approved"></div></section>
<section><h2>최근 기록</h2><div id="log"></div></section>
</div><script>
(function(){
  var $=function(i){return document.getElementById(i)};
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function act(op,id){ fetch('/admin/action',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Room-Admin':'1'},
    body:JSON.stringify({op:op,id:id})}).then(function(r){ if(r.status===401) location.href='/admin'; return r.json(); }).then(load); }
  window.__act=act;
  function load(){ fetch('/admin/data',{cache:'no-store',credentials:'same-origin'}).then(function(r){ if(r.status===401){location.href='/admin';throw 0;} return r.json(); }).then(function(d){
    $('pc').textContent='('+d.pending.length+')'; $('ac').textContent='('+d.approved.length+')';
    $('pending').innerHTML = d.pending.length ? '<table><tr><th>이름</th><th>확인 코드</th><th>대략적 위치</th><th>IP</th><th>기기·브라우저</th><th>요청 시각</th><th></th></tr>' +
      d.pending.map(function(r){ return '<tr><td>'+esc(r.name)+'</td><td class="code">'+esc(r.code)+'</td><td class="loc">'+esc(r.country||'국가 정보 없음')+
        (r.city?' · '+esc(r.city):'')+(r.region?' ('+esc(r.region)+')':'')+'<small>IP 기준이라 정확하지 않을 수 있음</small></td><td>'+esc(r.ip)+'</td><td title="'+esc(r.ua)+'">'+esc(r.device)+
        '</td><td>'+esc(r.created)+'</td><td><button class="ok" onclick="__act(\\'approve\\',\\''+r.rid+'\\')">승인</button><button class="no" onclick="__act(\\'reject\\',\\''+r.rid+'\\')">거절</button></td></tr>'; }).join('') + '</table>'
      : '<div class="empty">대기 중인 요청이 없습니다.</div>';
    $('approved').innerHTML = d.approved.length ? '<table><tr><th>이름</th><th>승인일</th><th>만료일</th><th>승인 때 위치·기기</th><th></th></tr>' +
      d.approved.map(function(a){ return '<tr><td>'+esc(a.name)+'</td><td>'+esc(a.approved)+'</td><td>'+esc(a.expires)+'</td><td>'+esc(a.where)+
        '</td><td><button class="no" onclick="__act(\\'revoke\\',\\''+a.aid+'\\')">승인 취소</button></td></tr>'; }).join('') + '</table>'
      : '<div class="empty">승인된 사람이 없습니다.</div>';
    $('log').innerHTML = d.log.map(function(e){ return '<div class="ev">'+esc(e.t.replace('T',' '))+' <b>'+esc(e.name)+'</b> '+esc(e.event)+(e.ip?' · '+esc(e.ip):'')+'</div>'; }).join('') || '<div class="empty">-</div>';
  }).catch(function(){}); }
  load(); setInterval(load,5000);
})();
</script></body></html>"""


def _admin_lock_msg(ip: str):
    now = time.time()
    if global_lock[0] > now:
        return f"관리자 로그인이 잠겨 있습니다(전체 실패 횟수 초과). {int((global_lock[0] - now) // 60) + 1}분 뒤에 다시 시도해 주세요."
    if ip_block.get(ip, 0) > now:
        return f"이 IP는 로그인 실패가 많아 차단되었습니다. {int((ip_block[ip] - now) // 60) + 1}분 뒤에 다시 시도해 주세요."
    return None


@app.get("/admin")
async def admin_home(req: Request):
    sweep()
    if not admin_of(req):
        return HTMLResponse(ADMIN_LOGIN.replace("__ERR__", ""), headers={"Cache-Control": "no-store"})
    return HTMLResponse(ADMIN_PAGE, headers={"Cache-Control": "no-store"})


@app.post("/admin/login")
async def admin_login(req: Request):
    ip = visitor(req)["ip"]
    msg = _admin_lock_msg(ip)
    if msg:
        log_access("관리자", "관리자 로그인 차단", ip)
        return HTMLResponse(ADMIN_LOGIN.replace("__ERR__", f'<div class="err">{html.escape(msg)}</div>'), status_code=429)
    form = urllib.parse.parse_qs((await req.body()).decode("utf-8"), keep_blank_values=True)
    aid, pw = (form.get("id") or [""])[0].strip(), (form.get("password") or [""])[0]
    acc = admin_account()
    ok = bool(acc) and bcrypt.checkpw(pw.encode()[:72], acc["bcrypt"].encode()) and hmac.compare_digest(aid, acc["id"])
    now = time.time()
    if not ok:
        fails = [t for t in ip_fails.get(ip, []) if now - t < IP_BLOCK] + [now]
        ip_fails[ip] = fails
        global_fails[:] = [t for t in global_fails if now - t < GLOBAL_WINDOW] + [now]
        if len(fails) >= IP_FAILS:
            ip_block[ip] = now + IP_BLOCK
        if len(global_fails) > GLOBAL_FAILS:
            global_lock[0] = now + GLOBAL_LOCK
        log_access("관리자", "관리자 로그인 실패", ip, fails_ip=len(fails))
        return HTMLResponse(ADMIN_LOGIN.replace("__ERR__", '<div class="err">아이디 또는 비밀번호가 맞지 않습니다.</div>'), status_code=401)
    ip_fails.pop(ip, None)
    log_access(f"{ADMIN_NAME}(관리자)", "관리자 로그인 성공", ip)
    resp = RedirectResponse("/admin", status_code=303)
    _cookie(resp, "room_admin", sign({"role": "admin", "id": acc["id"], "v": acc.get("v"), "sid": secrets.token_hex(6),
                                      "exp": now + ADMIN_DAYS * 86400}), ADMIN_DAYS)
    return resp


@app.post("/admin/logout")
async def admin_logout(req: Request):
    ad = admin_of(req)
    if ad:
        presence.pop("admin:" + ad["sid"], None)
        log_access(f"{ADMIN_NAME}(관리자)", "관리자 로그아웃", visitor(req)["ip"])
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("room_admin", path="/")
    return resp


@app.get("/admin/data")
async def admin_data(req: Request):
    sweep()
    if not admin_of(req):
        return JSONResponse({"error": "login"}, status_code=401)
    now = time.time()
    pending = [{k: r[k] for k in ("rid", "name", "code", "country", "region", "city", "ip", "device", "ua")} | {"created": fmt(r["created"])}
               for r in sorted(requests_.values(), key=lambda r: r["created"]) if r["status"] == "pending"]
    approved = [{"aid": a["aid"], "name": a["name"], "approved": fmt(a["approved_at"]), "expires": fmt(a["expires_at"]),
                 "where": " · ".join(x for x in (a.get("country"), a.get("city"), a.get("device")) if x)}
                for a in sorted(approvals.values(), key=lambda a: -a["approved_at"]) if not a.get("revoked_at") and a["expires_at"] > now]
    return JSONResponse({"pending": pending, "approved": approved, "log": recent_log(40)}, headers={"Cache-Control": "no-store"})


@app.post("/admin/action")
async def admin_action(req: Request):
    if not admin_of(req):
        return JSONResponse({"error": "login"}, status_code=401)
    if req.headers.get("x-room-admin") != "1":     # 다른 사이트에서 보낸 요청 막기
        return JSONResponse({"error": "bad request"}, status_code=400)
    body = await req.json()
    op, target = body.get("op"), str(body.get("id", ""))
    ip = visitor(req)["ip"]
    now = time.time()
    if op in ("approve", "reject"):
        r = requests_.get(target)
        if not r or r["status"] != "pending":
            return JSONResponse({"error": "이미 처리됐거나 만료된 요청"}, status_code=404)
        if op == "reject":
            r["status"] = "rejected"
            log_access(r["name"], "거절", r["ip"], by=ADMIN_NAME, code=r["code"])
        else:
            aid = secrets.token_urlsafe(12)
            approvals[aid] = {"aid": aid, "name": r["name"], "approved_at": now, "expires_at": now + USER_DAYS * 86400,
                              "revoked_at": None, "code": r["code"], **{k: r[k] for k in ("ip", "country", "region", "city", "device")}}
            save_approvals()
            r["status"], r["aid"] = "approved", aid
            log_access(r["name"], "승인", r["ip"], by=ADMIN_NAME, code=r["code"], expires=fmt(now + USER_DAYS * 86400))
        return JSONResponse({"ok": True})
    if op == "revoke":
        a = approvals.get(target)
        if not a or a.get("revoked_at"):
            return JSONResponse({"error": "없는 승인"}, status_code=404)
        a["revoked_at"] = now
        save_approvals()
        presence.pop("user:" + target, None)
        log_access(a["name"], "승인 취소", ip, by=ADMIN_NAME)
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "알 수 없는 작업"}, status_code=400)


# ---------- Studio 화면 위 접속자 상자 (관리자만 봄) ----------
WIDGET = r"""
(function () {
  if (window.__roomWidget) return; window.__roomWidget = true;
  var box = document.createElement('div');
  box.style.cssText = 'position:fixed;right:16px;bottom:96px;z-index:99999;width:230px;padding:8px 12px;' +
    'border-radius:10px;background:rgba(255,255,255,.95);color:#222;box-shadow:0 2px 10px rgba(0,0,0,.18);' +
    'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;cursor:move;user-select:none';
  box.title = '끌어서 옮길 수 있습니다';
  document.body.appendChild(box);
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function render(d) {
    var list = d.users.map(function (u) { return esc(u) + (u === d.me ? ' (나)' : ''); }).join(', ');
    var log = (d.log || []).slice(0, 5).map(function (e) { return '<div style="color:#555">' + e.t.slice(5, 16).replace('T', ' ') + ' ' + esc(e.name) + ' ' + esc(e.event) + '</div>'; }).join('');
    box.innerHTML = '<div style="font-weight:600;font-size:13px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#2fb344;margin-right:6px"></span>' +
      '접속 중 ' + d.users.length + '명</div><div>' + (list || '-') + '</div><div style="font-weight:600;margin-top:6px">접속 기록</div>' + (log || '-') +
      '<div style="margin-top:6px" onmousedown="event.stopPropagation()"><a href="/room" style="color:#2563eb;text-decoration:none">대화방</a> · ' +
      '<a href="/admin" style="color:#2563eb;text-decoration:none">관리자 화면</a></div>';
  }
  function tick() { fetch('/__room/presence', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); }).then(render).catch(function () {}); }
  tick(); setInterval(tick, 10000);
  var dx = 0, dy = 0, drag = false;
  box.addEventListener('mousedown', function (e) { drag = true; var r = box.getBoundingClientRect(); dx = e.clientX - r.left; dy = e.clientY - r.top; e.preventDefault(); });
  window.addEventListener('mousemove', function (e) { if (!drag) return; box.style.left = (e.clientX - dx) + 'px'; box.style.top = (e.clientY - dy) + 'px'; box.style.right = 'auto'; box.style.bottom = 'auto'; });
  window.addEventListener('mouseup', function () { drag = false; });
})();
"""


@app.get("/__room/widget.js")
async def widget():
    return Response(WIDGET, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "set-admin":
    import getpass
    p1 = getpass.getpass("새 관리자 비밀번호(20자 이상): ")
    if len(p1) < 20 or p1 != getpass.getpass("한 번 더: "):
        sys.exit("20자 이상이고 두 번 같아야 합니다.")
    set_admin(sys.argv[2], p1)
    print(f"관리자 '{sys.argv[2]}' 비밀번호를 바꿨습니다(해시만 저장). 입구 서비스를 다시 시작하세요: systemctl --user restart agent-room-gate")
