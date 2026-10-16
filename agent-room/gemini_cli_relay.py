"""Gemini 중계 (CLI 방식): 127.0.0.1:8092, OpenAI 형식 요청 → 공식 Gemini CLI 비대화형 모드(`gemini -p`).

API 키 무료 티어(gemini-3.7-flash 하루 20회) 대신 구글 계정 로그인(Gemini CLI) 한도를 쓴다.
- 시스템 프롬프트는 GEMINI_SYSTEM_MD(호출마다 임시 파일)로 Gemini CLI 기본 프롬프트(코딩 도우미)를 갈아 끼운다.
- 도구는 끈다: 빈 작업 폴더(gemini_cwd) + 그 폴더의 .gemini/settings.json에서 도구 목록을 비움.
- GEMINI_API_KEY 등 API 키 경로 환경변수는 지운다(로그인 계정 한도만 쓰게).
- 호출마다 CLI를 새로 띄우므로 느리다. 동시에 2개까지만 띄운다.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

ROOM = Path.home() / "agent-room"
CWD = ROOM / "gemini_cwd"
GEMINI_BIN = os.environ.get("GEMINI_BIN", "/home/dmlserver/.nvm/versions/node/v22.23.1/bin/gemini")
DEFAULT_MODEL = os.environ.get("GEMINI_CLI_MODEL", "gemini-3.8-flash")
# 무료 한도는 모델마다 하루 20회씩 따로다. 한도가 찬 모델은 초기화 때까지 건너뛰고 다음 모델로 넘어간다.
FALLBACK = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash", "gemini-3.5-flash-lite"]
TIMEOUT = 240
exhausted: dict[str, float] = {}  # 모델 → 한도 초기화 예상 시각(epoch)


class QuotaExhausted(RuntimeError):
    def __init__(self, model, retry_sec):
        super().__init__(f"{model} 하루 한도 소진 ({retry_sec / 3600:.1f}시간 뒤 초기화)")
        self.retry_sec = retry_sec


def _retry_sec(text: str) -> float:
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
    if not m or not any(m.groups()):
        return 6 * 3600
    h, mi, se = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + se

(CWD / ".gemini").mkdir(parents=True, exist_ok=True)
(CWD / ".gemini" / "settings.json").write_text(json.dumps({
    "tools": {"core": [], "exclude": ["run_shell_command", "write_file", "replace", "read_file", "read_many_files",
                                     "glob", "search_file_content", "list_directory", "web_fetch", "google_web_search",
                                     "save_memory"]},
    "mcpServers": {},
}, indent=1))

log = logging.getLogger("gemini_cli_relay")
_h = logging.FileHandler(ROOM / "logs" / "gemini_cli_relay.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
log.setLevel(logging.INFO)

app = FastAPI()
_sem = asyncio.Semaphore(2)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def build_prompt(messages):
    system, lines = [], []
    for m in messages:
        role, body = m.get("role"), _text(m.get("content")).strip()
        if role in ("system", "developer"):
            system.append(body)
            continue
        who = m.get("name") or ("나(이전 발언)" if role == "assistant" else "사용자")
        lines.append(f"[{who}]\n{body}")
    prompt = ("지금까지의 대화 기록이다. 기록을 이어서, 너의 다음 발언 하나만 작성하라. "
              "발언 앞에 화자 이름을 붙이지 마라.\n\n" + "\n\n".join(lines))
    return "\n\n".join(system), prompt


async def run_gemini(messages, model):
    system, prompt = build_prompt(messages)
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")}
    env["PATH"] = str(Path(GEMINI_BIN).parent) + ":" + env.get("PATH", "/usr/bin:/bin")
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir=ROOM / "logs", delete=False, encoding="utf-8") as f:
        f.write(system or "너는 대화 참여자다. 한국어로 간결하게 답한다.")
        sys_path = f.name
    env["GEMINI_SYSTEM_MD"] = sys_path
    t0 = time.time()
    try:
        async with _sem:
            proc = await asyncio.create_subprocess_exec(
                GEMINI_BIN, "-m", model, "-o", "json", "-p", prompt,
                cwd=str(CWD), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"gemini CLI {TIMEOUT}초 시간 초과")
    finally:
        os.unlink(sys_path)
    elapsed = time.time() - t0
    raw = out.decode("utf-8", "replace")
    allout = raw + err.decode("utf-8", "replace")
    if "exhausted your daily quota" in allout or "PerDay" in allout:
        raise QuotaExhausted(model, _retry_sec(allout))
    try:
        j = json.loads(raw[raw.index("{"):])
    except Exception:
        raise RuntimeError(f"gemini CLI 출력 해석 실패 (exit {proc.returncode}): {raw[-400:]} {err.decode('utf-8', 'replace')[-600:]}")
    if j.get("error"):
        raise RuntimeError(f"gemini CLI 에러: {json.dumps(j['error'], ensure_ascii=False)[:600]}")
    text = (j.get("response") or "").strip()
    models = (j.get("stats") or {}).get("models") or {}
    tok = {"prompt": 0, "candidates": 0}
    for m in models.values():
        t = m.get("tokens") or {}
        tok["prompt"] += int(t.get("prompt") or 0)
        tok["candidates"] += int(t.get("candidates") or 0)
    log.info(json.dumps({"model": model, "sec": round(elapsed, 2), "models_used": list(models), "tokens": tok,
                         "n_messages": len(messages)}, ensure_ascii=False))
    return text, {"prompt_tokens": tok["prompt"], "completion_tokens": tok["candidates"],
                  "total_tokens": tok["prompt"] + tok["candidates"]}


@app.get("/quota")
async def quota():
    now = time.time()
    return {m: (f"소진, {max(0, t - now) / 3600:.1f}시간 뒤 초기화" if t > now else "사용 가능") for m, t in
            ((m, exhausted.get(m, 0)) for m in FALLBACK)}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "gemini-cli"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    asked = body.get("model") or DEFAULT_MODEL
    order = ([asked] if asked not in FALLBACK else []) + FALLBACK[FALLBACK.index(asked) if asked in FALLBACK else 0:]
    text = usage = None
    for model in order:
        if exhausted.get(model, 0) > time.time():
            continue
        try:
            text, usage = await run_gemini(body.get("messages", []), model)
            break
        except QuotaExhausted as e:
            exhausted[model] = time.time() + e.retry_sec
            log.info(json.dumps({"model": model, "quota_exhausted": True, "retry_h": round(e.retry_sec / 3600, 1)}, ensure_ascii=False))
        except Exception as e:
            log.info(json.dumps({"model": model, "error": repr(e)}, ensure_ascii=False))
            return JSONResponse(status_code=502, content={"error": {"message": repr(e), "type": "gemini_cli_error"}})
    if text is None:
        soonest = min(exhausted.values()) - time.time()
        msg = f"제미나이 무료 한도 소진: 모든 모델({', '.join(order)})의 하루 한도를 다 썼습니다. 가장 빠른 초기화까지 약 {soonest / 3600:.1f}시간"
        log.info(json.dumps({"error": msg}, ensure_ascii=False))
        return JSONResponse(status_code=502, content={"error": {"message": msg, "type": "gemini_quota"}})
    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    if body.get("stream"):
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}]
        sse = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return Response(sse, media_type="text/event-stream")
    return {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage}
