"""Jev 발언자 선택 중계: 127.0.0.1:8094 (OpenAI 형식) → TypeSafe Jev.

팀 B(SelectorGroupChat)의 발언자 선택 모델로 연결한다. Studio는 파이썬 선택 함수를 저장하지 못해서
OpenAI 형식 엔드포인트로 만들었다. 선택 프롬프트(teams.SELECTOR_PROMPT)에 넣은 표시(@@ROLES@@ 등)로
역할·후보·대화 기록을 꺼낸 뒤 두 단계로 판단한다.
  ① 질문 유형 (대화마다 한 번, noul): 정답이 정해진 질문인가(정답형) / 판단이 필요한가(판단형)
  ② 다음 발언자 (매 발언, choice): 후보별 설명 = 질문 유형에 맞춘 규칙 → 확률이 가장 높은 후보
비용: 입력 100만 토큰당 0.042달러. 코싸인온과 같은 누적 장부(logs/jev_usage.json)·상한(3달러)을 쓴다 (2026-10-15 사용자 승인).
"""
import ast
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BASE_URL = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai/v1")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
LEDGER = Path(os.environ.get("JEV_LEDGER", "/home/dmlserver/cosignon-agent/logs/jev_usage.json"))
COST_CAP = float(os.environ.get("JEV_COST_CAP", "3"))
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000

ROOM = Path.home() / "agent-room"
log = logging.getLogger("jev_selector")
_h = logging.FileHandler(ROOM / "logs" / "jev_selector.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
log.setLevel(logging.INFO)

app = FastAPI()
client = httpx.AsyncClient(timeout=60)
_ledger_lock = threading.Lock()
_qtype_cache: dict[str, tuple[str, float]] = {}

# 질문 유형별 후보 설명 (Jev choice의 criteria). 키는 내부 이름.
RULES = {
    "정답형": {
        "claude": "제안 담당. 아직 아무 AI도 답을 내지 않았으면 이 참여자가 먼저 답을 제안한다.",
        "gemini": "검토 담당. 제안된 답에 사실 오류가 보일 때만 고른다. 오류가 없으면 고르지 않는다.",
        "deepseek": "정리 담당. 정답형 질문은 제안이 한 번 나왔고 명백한 오류가 없으면 바로 이 참여자가 최종안을 쓰고 끝낸다.",
        "human": "사람. 질문이 모호해서 사람에게 되물어야 할 때만 고른다.",
    },
    "판단형": {
        "claude": "제안 담당. 아직 제안이 없거나, 직전 발언이 제안의 약점을 지적했으면 이 참여자가 안을 고친다.",
        "gemini": "검토 담당. 직전 발언이 새 제안이나 수정안이면 이 참여자가 약점을 근거와 함께 지적한다.",
        "deepseek": "정리 담당. 제안과 검토가 합의에 이르렀거나(동의 표현), 같은 쟁점이 반복되면 이 참여자가 최종안을 쓴다.",
        "human": "사람. 방향을 정하려면 사람의 결정이나 정보가 꼭 필요할 때만 고른다.",
    },
}


def _ledger_read():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _ledger_add(usage):
    with _ledger_lock:
        d = _ledger_read()
        d["calls"] += 1
        d["input_tokens"] += int(usage.get("input_tokens", 0))
        d["output_tokens"] += int(usage.get("output_tokens", 0))
        d["cost_usd"] = round(d["input_tokens"] * PRICE_PER_INPUT_TOKEN, 6)
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(d), encoding="utf-8")


async def ask_jev(state: str, questions: dict) -> dict:
    if _ledger_read()["cost_usd"] >= COST_CAP:
        raise RuntimeError(f"Jev 누적 비용 상한 {COST_CAP}달러 도달")
    body = {"model": MODEL, "state": state, "questions": questions}
    for attempt in range(5):
        r = await client.post(f"{BASE_URL}/systemone", json=body,
                              headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"})
        if r.status_code not in (429, 529) or attempt == 4:
            break
        await asyncio.sleep(0.5 * 2 ** attempt)
    if r.status_code != 200:
        raise RuntimeError(f"Jev {r.status_code}: {r.text[:300]}")
    data = r.json()
    _ledger_add(data.get("usage") or {})
    return data["answers"]


def parse_prompt(text: str):
    roles = text.split("@@ROLES@@", 1)[1].split("@@CANDIDATES@@", 1)[0].strip()
    cands = ast.literal_eval(text.split("@@CANDIDATES@@", 1)[1].split("@@HISTORY@@", 1)[0].strip())
    history = text.split("@@HISTORY@@", 1)[1].split("@@END@@", 1)[0].strip()
    descs = {}
    for line in roles.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            descs[k.strip()] = v.strip()
    first = re.split(r"\n\s*\n", history, maxsplit=1)[0]
    task = first.split(":", 1)[1].strip() if first.startswith("user:") else first
    return descs, list(cands), history, task


async def question_type(task: str) -> tuple[str, float]:
    if task in _qtype_cache:
        return _qtype_cache[task]
    ans = await ask_jev(f"질문: {task}", {"qtype": {
        "type": "noul",
        "instructions": "이 질문은 정답이 하나로 정해져 있어서(사실·정의·반대말·계산 등) 토론 없이 바로 답할 수 있는 질문인가? "
                        "장단점을 따지거나 상황에 따라 판단해야 하는 질문이면 아니오.",
    }})
    p = float(ans["qtype"]["noul"])
    res = ("정답형" if p >= 0.5 else "판단형", p)
    _qtype_cache[task] = res
    return res


async def select(prompt_text: str) -> tuple[str, dict]:
    descs, cands, history, task = parse_prompt(prompt_text)
    qtype, p_fixed = await question_type(task)
    rules = RULES[qtype]
    criteria = {c: rules.get(c) or descs.get(c) or c for c in cands}
    ans = await ask_jev(f"[질문 유형: {qtype}]\n\n[대화 기록]\n{history}", {"next": {
        "type": "choice",
        "instructions": "여러 참여자가 질문에 대해 논의하고 있다. 대화 기록의 마지막 발언까지 읽고, 다음에 발언할 참여자를 하나 고르라. "
                        "보기 설명에 적힌 조건에 가장 잘 맞는 참여자를 고른다.",
        "criteria": criteria,
    }})
    probs = {str(k): float(v) for k, v in (ans["next"].get("probabilities") or {}).items()}
    if not probs and ans["next"].get("choice"):
        probs = {ans["next"]["choice"]: 1.0}
    choice = max((c for c in cands if c in probs), key=lambda c: probs[c], default=cands[0])
    return choice, {"qtype": qtype, "p_fixed": round(p_fixed, 3), "probs": {k: round(v, 3) for k, v in probs.items()}}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "jev-selector", "object": "model", "owned_by": "typesafe-jev"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    prompt_text = next((m["content"] for m in msgs if isinstance(m.get("content"), str) and "@@ROLES@@" in m["content"]), "")
    t0 = time.time()
    try:
        choice, info = await select(prompt_text)
    except Exception as e:
        log.info(json.dumps({"error": repr(e)}, ensure_ascii=False))
        return JSONResponse(status_code=502, content={"error": {"message": repr(e), "type": "jev_selector_error"}})
    info.update(choice=choice, sec=round(time.time() - t0, 2))
    log.info(json.dumps(info, ensure_ascii=False))
    return {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()), "model": "jev-selector",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": choice}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}}
