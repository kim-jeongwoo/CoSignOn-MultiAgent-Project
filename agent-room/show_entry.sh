#!/usr/bin/env bash
# 한 AI의 "대화방 입장 상태"를 터미널 한 화면(가로 80칸, 30줄 안)에 보여 준다. 발표 스크린샷용.
#   사용법: bash ~/agent-room/show_entry.sh claude|gemini|nvidia|jev
# 1 인증  2 중계 서버  3 팀 설정 등록(Studio)  4 연결 확인("까만색의 반대말은?")
# 키는 앞 6자리만, 이메일·토큰은 표시하지 않는다. gemini는 실행할 때마다 무료 한도 1회를 쓴다.
set -euo pipefail
case "${1:-}" in
  claude|gemini|nvidia|jev) ;;
  *) echo "사용법: bash ~/agent-room/show_entry.sh claude|gemini|nvidia|jev" >&2; exit 2 ;;
esac
export PATH="/home/dmlserver/.nvm/versions/node/v22.23.1/bin:$PATH"
exec "$HOME/agent-room/.venv/bin/python" - "$1" <<'PY'
import asyncio, json, os, re, subprocess, sys, time, unicodedata, urllib.request, warnings
from datetime import datetime

warnings.filterwarnings("ignore")  # AutoGen 모델 이름 불일치 경고(제미나이 자동 전환 시)가 화면 폭을 깨뜨리지 않게
from pathlib import Path

WHO = sys.argv[1]
ROOM = Path.home() / "agent-room"
STUDIO = "http://127.0.0.1:8081/api/teams/{}?user_id=guestuser@gmail.com"
INFO = {  # 표시 이름, 중계 주소, 서비스, Studio 팀 안 참여자 이름
    "claude": ("클로드", "http://127.0.0.1:8090/v1", "agent-room-claude", "claude"),
    "gemini": ("제미나이", "http://127.0.0.1:8092/v1", "agent-room-gemini", "gemini"),
    "nvidia": ("딥시크 (NVIDIA)", "http://127.0.0.1:8093/v1", "agent-room-nvidia", "deepseek"),
    "jev":    ("Jev (발언자 선택)", "http://127.0.0.1:8094/v1", "agent-room-jev", None),
}
KO, URL, SVC, NAME = INFO[WHO]
W = 80
B, C, G, R, Y, D, X = "\033[1m", "\033[1;36m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def cut(s, room):
    s = " ".join(str(s).split())
    if width(s) <= room:
        return s
    out = ""
    for ch in s:
        if width(out + ch) > room - 1:
            return out + "…"
        out += ch
    return out


def line(label, value="", color=""):
    pre = f"  {label}: " if label else "    "
    print(pre + color + cut(value, W - width(pre)) + (X if color else ""))


def head(n, title):
    print(f"{C}[{n}] {title}{X}")


def mask_key(v):
    v = v.strip().strip('"').strip("'")
    return (v[:6] + "*" * 10) if v else ""


def env_key(name):
    for l in (ROOM / ".env").read_text().splitlines():
        if l.startswith(name + "="):
            return l.split("=", 1)[1]
    return ""


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


title = f"━━ 대화방 입장 상태: {KO} ━"
stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
print(B + title + "━" * max(2, W - width(title) - width(stamp) - 1) + " " + stamp + X)

# 1. 인증
head(1, "인증")
if WHO == "claude":
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH"))}
    try:
        st = json.loads(subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, env=env, timeout=30).stdout)
    except Exception as e:
        st = {"error": repr(e)}
    ok = st.get("loggedIn")
    line("방식", f"{st.get('authMethod', '?')} 구독 로그인 → Agent SDK 크레딧 (API 키 아님)")
    line("상태", ("로그인됨" if ok else "로그인 안 됨") + f" · 구독 {st.get('subscriptionType', '?')} · 계정 이메일 (가림)", G if ok else R)
elif WHO == "gemini":
    try:
        sel = json.loads((Path.home() / ".gemini/settings.json").read_text())["security"]["auth"]["selectedType"]
    except Exception:
        sel = "?"
    desc = {"gemini-api-key": "Gemini API 키 (무료 티어, 모델별 하루 20회)", "oauth-personal": "구글 계정 로그인"}.get(sel, sel)
    cred = (Path.home() / ".gemini/gemini-credentials.json").exists()
    line("방식", f"Gemini CLI 인증 = {sel}")
    line("설명", desc + (" · 키 저장 파일 있음(암호화)" if cred else ""), G if sel != "?" else R)
else:
    var = "NVIDIA_API_KEY" if WHO == "nvidia" else "TYPESAFE_API_KEY"
    k = env_key(var)
    line("방식", f"API 키 ({'NVIDIA API 카탈로그 무료 키' if WHO == 'nvidia' else 'TypeSafe Jev, 코싸인온 3달러 장부 공유'})")
    line(var, (mask_key(k) + "  (~/agent-room/.env)") if k else "없음", G if k else R)

# 2. 중계 서버
head(2, "중계 서버")
state = subprocess.run(["systemctl", "--user", "is-active", SVC], capture_output=True, text=True).stdout.strip()
line("주소", URL)
line("서비스", f"{SVC}.service → {state}", G if state == "active" else R)

# 3. 팀 설정 등록
team_id = 2 if WHO == "jev" else 1
team = get_json(STUDIO.format(team_id))["data"]["component"]
head(3, f"팀 설정 등록 (Studio 팀 {team_id}: {team.get('label', '')})")
cfg = team["config"]
if WHO == "jev":
    mc = cfg["model_client"]["config"]
    line("자리", "SelectorGroupChat의 발언자 선택 모델(model_client)")
    line("모델", f"{mc.get('model')}  ·  연결 {mc.get('base_url')}")
    sp = [l for l in cfg.get("selector_prompt", "").splitlines() if l.strip()][:2]
    line("선택 지시문", sp[0] if sp else "")
    line("", sp[1] if len(sp) > 1 else "")
else:
    agent = next(p for p in cfg["participants"] if p["config"]["name"] == NAME)
    a = agent["config"]
    mc = a["model_client"]["config"]
    line("이름", f"{a['name']}  ·  모델 {mc.get('model')}")
    line("연결", mc.get("base_url"))
    sm = [l for l in (a.get("system_message") or "").splitlines() if l.strip()][:2]
    line("역할 지시문", sm[0] if sm else "")
    line("", sm[1] if len(sm) > 1 else "")

# 4. 연결 확인 — 진행 프로그램(AutoGen)과 같은 클라이언트·같은 OpenAI 형식
head(4, "연결 확인: \"까만색의 반대말은?\"")
from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage

Q = "까만색의 반대말은?"


async def probe():
    if WHO == "jev":
        client = ChatCompletionClient.load_component(cfg["model_client"])
        parts = [p["config"] for p in cfg["participants"]]
        roles = "\n".join(re.sub(r"\s+", " ", f"{p['name']}: {p.get('description', '')}").strip() for p in parts)
        prompt = cfg["selector_prompt"].format(roles=roles, participants=str([p["name"] for p in parts]),
                                               history=f"user: {Q}\n\n")
        msgs = [UserMessage(content=prompt, source="user")]
    else:
        client = ChatCompletionClient.load_component(agent["config"]["model_client"])
        msgs = [SystemMessage(content=a.get("system_message") or ""), UserMessage(content=Q, source="user")]
    t0 = time.time()
    r = await client.create(msgs)
    return r.content if isinstance(r.content, str) else str(r.content), time.time() - t0


try:
    text, sec = asyncio.run(probe())
    extra = ""
    if WHO == "gemini":
        last = (ROOM / "logs/gemini_cli_relay.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        m = re.search(r'"models_used": \["([^"]+)"', last)
        extra = f" · 실제 모델 {m.group(1)}" if m else ""
    if WHO == "jev":
        last = (ROOM / "logs/jev_selector.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        d = json.loads(last[last.index("{"):])
        extra = f" · 질문 유형 {d.get('qtype')}({d.get('p_fixed')})"
    line("결과", f"응답 받음 · 걸린 시간 {sec:.1f}초{extra}", G)
    shown = [l for l in text.strip().splitlines() if l.strip()][:3]
    if WHO == "jev":
        shown = [f"다음 발언자 → {text.strip()}"]
    for l in shown:
        print(f"  {D}│{X} " + cut(l, W - 4))
except Exception as e:
    msg = str(e)
    line("결과", "실패: " + (("제미나이 무료 한도 소진" if "한도" in msg or "quota" in msg.lower() else msg)), R)
PY
