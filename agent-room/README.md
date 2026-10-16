# agent-room — AI 대화방 코드

AutoGen Studio에서 클로드·제미나이·딥시크 세 AI가 한 대화방에서 논의하고, 사람이 웹 화면에서 함께 참여하는 실험 환경입니다.
모든 경로는 원래 서버 기준(`/home/dmlserver/agent-room`)으로 적혀 있습니다. 다른 곳에 설치하면 서비스 파일과 코드 안의 경로를 바꿔야 합니다.

## 구성 요소

### 중계 서버 4개 (모두 OpenAI 형식 `/v1/chat/completions`, `127.0.0.1`에서만 열림)

| 파일 | 포트 | 서비스 | 하는 일 |
|---|---|---|---|
| `claude_proxy.py` | 8090 | `agent-room-claude` | `claude-agent-sdk`로 Claude 호출(서버의 Claude 구독 로그인 사용, API 키 아님). 도구 없음, `max_turns=1`. 모델은 요청의 `model` 값으로 `sonnet`/`haiku`. claude.ai 계정 커넥터는 끔 |
| `gemini_cli_relay.py` | 8092 | `agent-room-gemini` | 공식 Gemini CLI를 비대화형(`gemini -p`)으로 호출. 시스템 프롬프트는 `GEMINI_SYSTEM_MD`로 교체, 도구는 끔. 모델별 하루 한도가 차면 `gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-2.5-flash` → `gemini-3.5-flash-lite` 순으로 넘어감. 한도 상태: `GET /quota` |
| `nvidia_relay.py` | 8093 | `agent-room-nvidia` | NVIDIA API 카탈로그(`integrate.api.nvidia.com`) 중계. 기본 모델 `deepseek-ai/deepseek-v4.1-flash`, 추론 과정(`reasoning_content`)은 걸러냄 |
| `jev_selector.py` | 8094 | `agent-room-jev` | 팀 B의 다음 발언자 선택. TypeSafe Jev로 ① 질문 유형(정답형/판단형, 대화마다 1회) ② 다음 발언자(매 발언)를 판단. 누적 비용 장부와 상한은 `JEV_LEDGER`·`JEV_COST_CAP` |

### 대화방 화면과 로그인

| 파일 | 내용 |
|---|---|
| `room_gate.py` | 포트 8095, 서비스 `agent-room-gate`. 로그인 페이지(`/__room/login`), 대화방 화면(`/room`), 접속자·접속 기록(`/__room/presence`), nginx `auth_request` 확인(`/auth`). 이름은 아무거나, 비밀번호는 환경변수 `ROOM_PASSWORD`(비어 있으면 로그인 불가). 한글 자판으로 친 비밀번호도 영문으로 바꿔 비교. 세션 쿠키 + 3분 동안 신호가 없으면 자동 퇴장 |
| `room_chat.html` | `/room` 화면. 내 말은 오른쪽, AI는 왼쪽 말풍선. Studio API(`/api/teams`, `/api/sessions`, `/api/runs`)와 웹소켓(`/api/ws/runs/{id}`)을 그대로 씀. 같은 대화의 앞 발언을 다음 질문에 붙여 보내서 이어서 대화함("새 대화" 버튼으로 비움) |
| `nginx/nginx.conf` | 사용자 권한 nginx, 포트 8082 → Studio 8081. 로그인 안 된 요청은 로그인 페이지로 보내고, Studio 화면에는 접속자 상자(`/__room/widget.js`)를 끼워 넣음 |

### AutoGen Studio와 팀 설정

| 파일 | 내용 |
|---|---|
| `teams.py` | 참여자 3명(claude 제안, gemini 검토, deepseek 정리) + 사람(human), 공통 발언 규칙, 종료 조건(`합의완료` 언급 또는 메시지 16개). 팀 A = `RoundRobinGroupChat`(claude → gemini → deepseek → human), 팀 B = `SelectorGroupChat`(선택 모델 = jev_selector) |
| `build_teams.py` | `teams.py`로 `teams/team_a.json`, `teams/team_b.json`(AutoGen 컴포넌트 JSON) 생성. JSON 안 `api_key`는 자리 값(`**********`)이고 실제 키는 각 중계 서버 환경에만 있음 |
| `teams/*.json` | Studio에 등록한 팀 설정 |

Studio 실행 명령(`agent-room-studio` 서비스): `autogenstudio ui --host 127.0.0.1 --port 8081 --appdir /home/dmlserver/agent-room/studio`

외부 접속은 `agent-room-tunnel` 서비스(cloudflared, 설정 파일 `~/.cloudflared/config-agent-room.yml` — 저장소에 없음)가 nginx(8082)로 연결합니다.

### 기타

- `show_entry.sh` — AI 하나의 "대화방 입장 상태" 화면(아래 사용법)
- `results/` — 자동 비교 실험 결과(팀 A/B × 질문 2개 × 2회)
- `VERSIONS.md` — 패키지·모델 버전
- `systemd/` — 사용자 서비스 파일

## 필요한 환경변수

`~/agent-room/.env`에 둡니다(저장소에 없음). `agent-room-nvidia`, `agent-room-jev`, `agent-room-studio` 서비스가 `EnvironmentFile`로 읽습니다.

| 이름 | 쓰는 곳 |
|---|---|
| `NVIDIA_API_KEY` | `nvidia_relay.py` |
| `TYPESAFE_API_KEY` | `jev_selector.py` |

| `ROOM_PASSWORD` | `room_gate.py` — 대화방 비밀번호. `systemd/agent-room-gate.service`의 `Environment=` 줄에 넣음 |

선택(코드에 기본값 있음): `NVIDIA_MODEL`, `GEMINI_CLI_MODEL`, `GEMINI_BIN`, `JEV_BASE_URL`, `JEV_MODEL`, `JEV_LEDGER`, `JEV_COST_CAP`.
`agent-room-claude` 서비스는 `ENABLE_CLAUDEAI_MCP_SERVERS=false`를 넣고 `ANTHROPIC_API_KEY` 등은 지웁니다.

환경변수가 아닌 인증:
- Claude: 서버에서 `claude` CLI로 구독 계정 로그인
- Gemini: Gemini CLI 자체 인증(`~/.gemini/settings.json`의 `security.auth.selectedType`)

## 실행과 재시작

서비스 파일을 사용자 서비스 폴더에 두고 켭니다.

```bash
cp systemd/agent-room-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-room-claude agent-room-gemini agent-room-nvidia agent-room-jev agent-room-gate agent-room-studio agent-room-nginx agent-room-tunnel
```

재부팅 뒤에도 유지하려면 `loginctl enable-linger $USER`가 켜져 있어야 합니다.

```bash
systemctl --user restart agent-room-gemini          # 하나만 다시 시작
systemctl --user is-active agent-room-claude agent-room-gemini agent-room-nvidia agent-room-jev agent-room-gate agent-room-studio agent-room-nginx agent-room-tunnel
systemctl --user reload agent-room-nginx            # nginx.conf만 바꿨을 때
```

팀 설정을 바꾼 뒤에는 `build_teams.py`로 JSON을 다시 만들고 Studio에 다시 등록합니다(Studio 화면의 팀 가져오기, 또는 Studio API `POST /api/teams/`).

## show_entry.sh 사용법

```bash
bash ~/agent-room/show_entry.sh claude|gemini|nvidia|jev
```

한 AI의 상태를 가로 80칸 한 화면에 보여 줍니다.
1. 인증 방식(키는 앞 6자리만, 이메일은 가림)
2. 중계 서버 주소와 서비스 상태
3. Studio 팀 설정에 등록된 이름·모델·연결 주소·역할 지시문 앞 2줄(Jev는 팀 B의 선택 모델 자리)
4. 연결 확인 — AutoGen과 같은 클라이언트로 "까만색의 반대말은?"을 보내 받은 답 앞 3줄과 걸린 시간

`gemini`는 실행할 때마다 Gemini 무료 한도를 1회 씁니다.
