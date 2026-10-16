"""Claude 중계 서버: OpenAI 형식 요청 → claude_agent_sdk.query (구독 Agent SDK 크레딧).

- ANTHROPIC_API_KEY 등 종량 결제 경로 환경변수는 시작 시 제거한다.
- 도구 없음, max_turns=1, 사용자/프로젝트 설정(CLAUDE.md 등) 불러오지 않음.
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path

for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
    os.environ.pop(_k, None)

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

ROOM = Path.home() / "agent-room"
SANDBOX = ROOM / "claude_cwd"
SANDBOX.mkdir(exist_ok=True)
(ROOM / "logs").mkdir(exist_ok=True)

log = logging.getLogger("claude_proxy")
_h = logging.FileHandler(ROOM / "logs" / "claude_proxy.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
log.setLevel(logging.INFO)

MODELS = {"sonnet": "sonnet", "haiku": "haiku"}
app = FastAPI()


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
        if role == "assistant":
            who = m.get("name") or "나(이전 발언)"
        else:
            who = m.get("name") or "사용자"
        lines.append(f"[{who}]\n{body}")
    prompt = (
        "지금까지의 대화 기록이다. 기록을 이어서, 너의 다음 발언 하나만 작성하라. "
        "발언 앞에 화자 이름을 붙이지 마라.\n\n" + "\n\n".join(lines)
    )
    return "\n\n".join(system) or None, prompt


async def run_claude(messages, model_key):
    system, prompt = build_prompt(messages)
    opts = ClaudeAgentOptions(
        system_prompt=system,
        model=MODELS.get(model_key, "sonnet"),
        tools=[],
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
        # claude.ai 계정 커넥터(Notion·Drive 등 도구 78개)가 붙으면 호출 하나가 입력 10만 토큰(약 0.40달러)이 된다 → 끈다.
        strict_mcp_config=True,
        mcp_servers={},
        disallowed_tools=["mcp__*"],
        cwd=str(SANDBOX),
        env={"ANTHROPIC_API_KEY": "", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )
    t0 = time.time()
    texts, result = [], None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage):
            result = msg
    elapsed = time.time() - t0
    if result is not None and result.is_error:
        raise RuntimeError(f"SDK error: subtype={result.subtype} status={result.api_error_status} result={result.result} errors={result.errors}")
    text = (result.result if result and result.result else "\n".join(texts)).strip()
    u = (result.usage or {}) if result else {}
    inp = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    out = u.get("output_tokens") or 0
    cost = result.total_cost_usd if result else None
    log.info("raw_usage " + json.dumps({"usage": u, "model_usage": result.model_usage if result else None}, ensure_ascii=False, default=str))
    log.info(json.dumps({"model": model_key, "sec": round(elapsed, 2), "input_tokens": inp, "output_tokens": out,
                         "cost_usd": cost, "n_messages": len(messages)}, ensure_ascii=False))
    return text, {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": k, "object": "model", "owned_by": "anthropic-agent-sdk"} for k in MODELS]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    model_key = body.get("model", "sonnet")
    model_key = model_key if model_key in MODELS else "sonnet"
    try:
        text, usage = await run_claude(body.get("messages", []), model_key)
    except Exception as e:  # 에러 원문을 그대로 돌려준다
        log.info(json.dumps({"model": model_key, "error": repr(e)}, ensure_ascii=False))
        return JSONResponse(status_code=502, content={"error": {"message": repr(e), "type": "claude_agent_sdk_error"}})
    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    if body.get("stream"):
        def sse():
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_key}
            yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")
    return {"id": cid, "object": "chat.completion", "created": created, "model": model_key,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage}
