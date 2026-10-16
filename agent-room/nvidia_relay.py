"""NVIDIA API 카탈로그 중계: 127.0.0.1:8093 → integrate.api.nvidia.com (OpenAI 호환).

키(NVIDIA_API_KEY)는 이 프로세스 환경변수에만 둔다. 팀 JSON·Studio DB에는 키가 없다.
무료 대기열이 느려서 시간 제한을 넉넉히 두고, 429·5xx는 잠깐 뒤 다시 시도한다.
추론 과정(reasoning_content)은 AutoGen에 넘기지 않는다.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

UPSTREAM = "https://integrate.api.nvidia.com/v1"
MODEL = os.environ.get("NVIDIA_MODEL", "deepseek-ai/deepseek-v4.1-flash")
ROOM = Path.home() / "agent-room"
log = logging.getLogger("nvidia_relay")
_h = logging.FileHandler(ROOM / "logs" / "nvidia_relay.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
log.setLevel(logging.INFO)

app = FastAPI()
client = httpx.AsyncClient(timeout=300)


def _headers():
    return {"Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}", "Content-Type": "application/json"}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "nvidia-api-catalog"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    payload = await req.json()
    payload["model"] = MODEL
    payload.pop("stream_options", None)
    stream = payload.pop("stream", False)
    payload.setdefault("max_tokens", 2000)
    for attempt in range(4):
        t0 = time.time()
        try:
            r = await client.post(f"{UPSTREAM}/chat/completions", headers=_headers(), json=payload)
        except httpx.HTTPError as e:
            log.info(json.dumps({"error": repr(e), "attempt": attempt}))
            if attempt == 3:
                return Response(json.dumps({"error": {"message": repr(e)}}), status_code=504, media_type="application/json")
            await asyncio.sleep(5 * (attempt + 1))
            continue
        log.info(json.dumps({"model": MODEL, "status": r.status_code, "sec": round(time.time() - t0, 2), "attempt": attempt}))
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        return Response(r.content, status_code=r.status_code, media_type="application/json")
    j = r.json()
    for c in j.get("choices", []):
        c.get("message", {}).pop("reasoning_content", None)
    if not stream:
        return j
    msg = j["choices"][0]["message"]
    base = {"id": j.get("id", "nvidia"), "object": "chat.completion.chunk", "created": j.get("created", int(time.time())), "model": MODEL}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg.get("content") or ""}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": j.get("usage")},
    ]
    sse = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return Response(sse, media_type="text/event-stream")
