"""대화방 입구: 127.0.0.1:8095 — nginx auth_request 대상 + 로그인 페이지 + 대화방 페이지(/room) + 접속자·접속 기록.

- 로그인: 직접 만든 로그인 페이지(/__room/login). 이름은 아무거나(예: 김정우), 비밀번호는 환경변수 ROOM_PASSWORD
  (비어 있으면 아무도 로그인할 수 없다).
  브라우저 기본 로그인 창은 맥 크롬에서 한글이 자모로 쪼개져 입력돼서 쓰지 않는다.
  한글 자판 상태로 친 비밀번호도 받는다(예: 비밀번호가 test면 'ㅅㄷㄴㅅ'도 통과). 대소문자도 가리지 않는다.
- 매번 로그인: 쿠키는 브라우저를 닫으면 지워지는 세션 쿠키이고, 서버도 IDLE_SEC초 동안 화면 신호(10초마다)가
  없으면 세션을 끊는다. 창을 닫았다가 다시 열면 로그인부터 한다.
- 접속자: ACTIVE_SEC초 안에 신호가 있었던 이름. 접속 기록: logs/access_log.jsonl (입장·나가기·자동 퇴장).
- 서버 안 스크립트용으로 Basic 인증 헤더(이름:room)도 받는다(외부 요청은 거절, 접속 기록에는 남기지 않음).
"""
import base64
import html
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

PASSWORD = os.environ.get("ROOM_PASSWORD", "")
ACTIVE_SEC = 30
IDLE_SEC = 180
ROOM = Path.home() / "agent-room"
ACCESS_LOG = ROOM / "logs" / "access_log.jsonl"
CHAT_HTML = ROOM / "room_chat.html"
app = FastAPI()
sessions: dict[str, dict] = {}      # 쿠키 토큰 → {name, last, ip}
script_seen: dict[str, float] = {}  # Basic 인증(스크립트) 이름 → 마지막 요청 시각

# 한글 2벌식 자판 → 영문 키 (한글 상태로 친 비밀번호를 영문으로 되돌린다)
_CHO = "r R s e E f a q Q t T d w W c z x v g".split()
_JUNG = "k o i O j p u P h hk ho hl y n nj np nl b m ml l".split()
_JONG = [""] + "r R rt s sw sg e f fr fa fq ft fx fv fg a q qt t T d w c z x v g".split()
_COMPAT = dict(zip("ㄱㄲㄳㄴㄵㄶㄷㄸㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅃㅄㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ",
                   "r R rt s sw sg e E f fr fa fq ft fx fv fg a q Q qt t T d w W c z x v g".split()))
_COMPAT.update(zip("ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ", _JUNG))


def to_keys(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out += [_CHO[code // 588], _JUNG[(code % 588) // 28], _JONG[code % 28]]
        else:
            out.append(_COMPAT.get(ch, ch))
    return "".join(out)


def password_ok(pw: str) -> bool:
    return bool(PASSWORD) and to_keys(pw.strip()).lower() == PASSWORD.lower()


def _clean(name: str) -> str:
    return " ".join(name.split())[:20]


def _ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0] or ""


def log_access(name: str, event: str, ip: str = ""):
    ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACCESS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "name": name, "event": event, "ip": ip},
                           ensure_ascii=False) + "\n")


def sweep():
    now = time.time()
    for tok, s in list(sessions.items()):
        if now - s["last"] > IDLE_SEC:
            sessions.pop(tok, None)
            log_access(s["name"], "자동 퇴장(화면 닫힘)", s.get("ip", ""))


def user_of(req: Request, touch=False):
    sweep()
    tok = req.cookies.get("room_sid")
    if tok and tok in sessions:
        if touch:
            sessions[tok]["last"] = time.time()
        return sessions[tok]["name"]
    h = req.headers.get("authorization", "")
    # Basic 인증은 서버 안 스크립트용. 외부(Cloudflare 터널)로 온 요청은 받지 않는다 —
    # 예전 브라우저 기본 로그인 창의 인증 정보가 남아 로그인 페이지·접속 기록을 건너뛰는 일을 막는다.
    if h.lower().startswith("basic ") and not req.headers.get("cf-connecting-ip"):
        try:
            raw = base64.b64decode(h[6:].strip())
            text = raw.decode("utf-8")
        except Exception:
            return None
        name, sep, pw = text.partition(":")
        name = _clean(name)
        if sep and name and password_ok(pw):
            if touch:
                script_seen[name] = time.time()
            return name
    return None


@app.get("/auth")
async def auth(req: Request):
    name = user_of(req, touch=True)
    if name is None:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Room-User": urllib.parse.quote(name)})


def _safe_next(nxt: str) -> str:
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/__room/log") or nxt == "/":
        return "/room"
    return nxt


LOGIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>agent-room 입장</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d1f23;--muted:#6b7280;--line:#d6d9de;--accent:#2563eb;--err:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#20232a;--fg:#e8eaed;--muted:#9aa0a6;--line:#3a3f47;--accent:#5b8def;--err:#ff7b6b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;padding:16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}
h1{font-size:20px;margin:0 0 4px}p.sub{margin:0 0 20px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font-size:15px}
input:focus{outline:2px solid var(--accent);border-color:transparent}
button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.err{color:var(--err);font-size:13px;margin-top:10px}
</style></head><body>
<form method="post" action="/__room/login">
<h1>agent-room</h1><p class="sub">클로드 · 제미나이 · 딥시크 대화방</p>
<input type="hidden" name="next" value="{next}">
<label for="n">이름</label><input id="n" name="name" required maxlength="20" placeholder="예: 김정우" value="{name}" autofocus autocomplete="off">
<label for="p">비밀번호</label><input id="p" name="password" type="password" required autocomplete="off">
{err}<button type="submit">들어가기</button>
</form></body></html>"""


def _login_html(nxt="/room", name="", err=""):
    return LOGIN_PAGE.replace("{next}", html.escape(nxt)).replace("{name}", html.escape(name)).replace(
        "{err}", f'<div class="err">{html.escape(err)}</div>' if err else "")


@app.get("/__room/login")
async def login_form(next: str = "/room"):
    return HTMLResponse(_login_html(_safe_next(next)), headers={"Cache-Control": "no-store"})


@app.post("/__room/login")
async def login_submit(req: Request):
    form = urllib.parse.parse_qs((await req.body()).decode("utf-8"), keep_blank_values=True)
    name = _clean((form.get("name") or [""])[0])
    pw = (form.get("password") or [""])[0]
    nxt = _safe_next((form.get("next") or ["/room"])[0])
    if not name or not password_ok(pw):
        return HTMLResponse(_login_html(nxt, name, "이름을 넣고 비밀번호를 확인해 주세요."), status_code=401)
    tok = secrets.token_urlsafe(24)
    sessions[tok] = {"name": name, "last": time.time(), "ip": _ip(req)}
    log_access(name, "입장", _ip(req))
    r = RedirectResponse(nxt, status_code=303)
    r.set_cookie("room_sid", tok, httponly=True, secure=True, samesite="lax", path="/")  # 세션 쿠키(만료 없음)
    return r


@app.get("/__room/logout")
async def logout(req: Request):
    tok = req.cookies.get("room_sid")
    s = sessions.pop(tok, None) if tok else None
    if s:
        log_access(s["name"], "나가기", s.get("ip", ""))
    r = RedirectResponse("/__room/login", status_code=303)
    r.delete_cookie("room_sid", path="/")
    return r


def _recent_log(n=30):
    if not ACCESS_LOG.exists():
        return []
    lines = ACCESS_LOG.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for l in reversed(lines):
        try:
            d = json.loads(l)
            out.append({"t": d["t"], "name": d["name"], "event": d["event"]})
        except Exception:
            pass
    return out


@app.get("/__room/presence")
async def presence(req: Request):
    me = user_of(req, touch=True)
    now = time.time()
    names = {s["name"] for s in sessions.values() if now - s["last"] <= ACTIVE_SEC}
    names |= {n for n, t in script_seen.items() if now - t <= ACTIVE_SEC}
    users = sorted(names, key=lambda n: (n != me, n))
    return JSONResponse({"me": me, "users": users, "log": _recent_log()}, headers={"Cache-Control": "no-store"})


@app.get("/room")
async def room():
    return HTMLResponse(CHAT_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


WIDGET = r"""
(function () {
  if (window.__roomWidget) return; window.__roomWidget = true;
  var box = document.createElement('div');
  box.id = 'room-presence';
  box.style.cssText = 'position:fixed;right:16px;bottom:96px;z-index:99999;width:230px;padding:8px 12px;' +
    'border-radius:10px;background:rgba(255,255,255,.95);color:#222;box-shadow:0 2px 10px rgba(0,0,0,.18);' +
    'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;cursor:move;user-select:none';
  box.title = '끌어서 옮길 수 있습니다';
  document.body.appendChild(box);
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function hm(t) { return t.slice(5, 16).replace('T', ' '); }
  function render(d) {
    var list = d.users.map(function (u) { return esc(u) + (u === d.me ? ' (나)' : ''); }).join(', ');
    var log = (d.log || []).slice(0, 5).map(function (e) { return '<div style="color:#555">' + hm(e.t) + ' ' + esc(e.name) + ' ' + esc(e.event) + '</div>'; }).join('');
    box.innerHTML = '<div style="font-weight:600;font-size:13px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#2fb344;margin-right:6px"></span>' +
      '접속 중 ' + d.users.length + '명</div><div>' + (list || '-') + '</div>' +
      '<div style="font-weight:600;margin-top:6px">접속 기록</div>' + (log || '-') +
      '<div style="margin-top:6px" onmousedown="event.stopPropagation()"><a href="/room" style="color:#2563eb;text-decoration:none">대화방(말풍선)</a> · ' +
      '<a href="/__room/logout" style="color:#2563eb;text-decoration:none">나가기</a></div>';
  }
  function tick() {
    fetch('/__room/presence', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { if (r.redirected || r.status !== 200) { location.href = '/__room/login?next=' + encodeURIComponent(location.pathname); throw 0; } return r.json(); })
      .then(render).catch(function () {});
  }
  tick(); setInterval(tick, 10000);
  var dx = 0, dy = 0, drag = false;
  box.addEventListener('mousedown', function (e) { drag = true; var r = box.getBoundingClientRect(); dx = e.clientX - r.left; dy = e.clientY - r.top; e.preventDefault(); });
  window.addEventListener('mousemove', function (e) {
    if (!drag) return;
    box.style.left = (e.clientX - dx) + 'px'; box.style.top = (e.clientY - dy) + 'px';
    box.style.right = 'auto'; box.style.bottom = 'auto';
  });
  window.addEventListener('mouseup', function () { drag = false; });
})();
"""


@app.get("/__room/widget.js")
async def widget():
    return Response(WIDGET, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})
