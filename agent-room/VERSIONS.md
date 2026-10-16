# agent-room 버전 기록 (2026-10-15 갱신)

## 파이썬 패키지 (.venv, Python 3.12.14)

- autogenstudio 0.4.2.2
- autogen-agentchat 0.5.7
- autogen-core 0.5.7
- autogen-ext 0.5.7
- openai 3.31.0
- claude-agent-sdk 0.2.167
- fastapi 0.143.1
- uvicorn 0.54.0
- httpx 0.28.1
- websockets 17.2

## 모델

- 클로드: Claude Sonnet (Agent SDK 별칭 'sonnet' → claude-sonnet-5-5), 'haiku'도 선택 가능. Agent SDK 번들 CLI 사용, 구독(Max) 로그인
- 제미나이: gemini-3.8-flash 우선 — 공식 Gemini CLI 0.64.0 비대화형(-p), 구글 계정 로그인 한도 (gemini_cli_relay 8092). 모델마다 하루 20회라 한도가 차면 3.7-flash → 2.5-flash → 3.5-flash-lite 순으로 자동 전환(상태: curl 127.0.0.1:8092/quota). API 키 무료 티어(gemini-3.7-flash 하루 20회)는 한도 때문에 중단
- 딥시크: deepseek-ai/deepseek-v4.1-flash — NVIDIA API 카탈로그 무료 키 (nvidia_relay 8093, 추론 켬·추론 과정은 걸러냄)
- 팀 B 발언자 선택: TypeSafe Jev(jev-latest) (jev_selector 8094, 코싸인온 Jev 장부·3달러 상한 공유)
- (중단) 로컬 Qwen3.5-9B-Q8_0 — 대화 참여자에서 뺌, agent-room-local 서비스 끔

## 기타

- nginx version: nginx/1.28.0 (Ubuntu) (사용자 권한 인스턴스, basic auth)
- cloudflared version 2026.7.1 (built 2026-07-09-13:00 UTC) (전용 터널 agent-room)
