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
| `room_gate.py` | 포트 8095, 서비스 `agent-room-gate`. 관리자 승인 방식 로그인(아래 "승인 로그인"), 승인 요청 페이지(`/__room/request`), 관리자 화면(`/admin`), 대화방 화면(`/room`), 접속자·접속 기록(`/__room/presence`), nginx `auth_request` 권한 확인(`/auth`) |
| `room_chat.html` | `/room` 화면. 내 말은 오른쪽, AI는 왼쪽 말풍선. Studio API(`/api/teams`, `/api/sessions`, `/api/runs`)와 웹소켓(`/api/ws/runs/{id}`)을 그대로 씀. 같은 대화의 앞 발언을 다음 질문에 붙여 보내서 이어서 대화함("새 대화" 버튼으로 비움) |
| `nginx/nginx.conf` | 사용자 권한 nginx, 포트 8082 → Studio 8081. `/__room/*`·`/room`·`/admin`은 room_gate가 직접 처리하고, 그 밖(Studio 화면·API·웹소켓)은 room_gate `/auth`로 권한을 확인함. 승인이 없으면 승인 요청 페이지로, 권한이 없으면 `/room`으로 보냄. Studio 화면에는 접속자 상자(`/__room/widget.js`)를 끼워 넣음 |

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

## 승인 로그인

대화방은 비밀번호 대신 관리자 승인으로 들어갑니다(`room_gate.py`).

1. 승인 쿠키가 없는 사람은 승인 요청 페이지(`/__room/request`)로 갑니다. 이름을 넣고 "입장 요청"을 누르면 6자리 확인 코드가 뜨고, 이 코드를 관리자에게 알립니다. 요청에는 Cloudflare가 넘겨주는 방문자 위치 헤더(`CF-IPCountry`, `cf-ipcity`, `cf-region`)와 IP, 기기·브라우저가 함께 기록됩니다(외부 위치 조회 서비스는 쓰지 않음). 도시·지역은 Cloudflare의 Managed Transforms → "Add visitor location headers"가 켜져 있어야 들어옵니다.
2. 요청 화면은 3초마다 승인 여부를 확인하다가, 승인되면 `/room`으로 이동합니다.
3. 관리자는 `/admin`에서 대기 중 요청(이름·코드·대략적 위치·IP·기기·시각)을 보고 승인하거나 거절합니다. 승인된 사람 목록에서 승인 취소도 합니다.
4. 승인되면 그 브라우저에 30일짜리 쿠키(`room_auth`, 서버 서명 키로 HMAC 서명, HttpOnly·Secure·SameSite=Lax)가 발급됩니다. 서버의 승인 목록과 함께 확인하므로 승인 취소는 바로 반영됩니다. 30일이 지나면 다시 요청합니다.
5. 권한: 승인된 일반 사용자는 대화방(`/room`)과 대화에 필요한 Studio API(`GET /api/teams/`, `POST /api/sessions/`, `POST /api/runs/`, 웹소켓 `/api/ws/runs/{id}`)만 씁니다. Studio 화면(팀 편집)과 그 밖의 API는 관리자만 씁니다.

제한과 기록:
- 같은 IP의 승인 요청은 10분에 3번까지, 처리되지 않은 요청은 1시간 뒤 자동 만료
- 관리자 로그인: 같은 IP에서 5번 실패하면 30분 차단, 전체 실패가 1시간에 20번을 넘으면 관리자 로그인 1시간 잠금. 관리자 세션 쿠키(`room_admin`)는 7일
- 차단 상태와 대기 중 요청은 메모리에만 있어서 `systemctl --user restart agent-room-gate`로 지워집니다. 관리자 계정과 승인 목록은 파일에 남습니다
- 접속 기록(`~/agent-room/logs/access_log.jsonl`): 승인 요청·승인·거절·승인 취소·요청 만료·입장·나가기·자동 퇴장·관리자 로그인 성공/실패/차단

서버에만 있고 저장소에 없는 파일(직접 만들어짐):
- `~/agent-room/gate_state/admin.json` — 관리자 아이디와 bcrypt 해시(권한 600)
- `~/agent-room/gate_state/approvals.json` — 승인 목록
- `~/agent-room/.gate_secret` — 쿠키 서명 키(없으면 처음 실행할 때 무작위로 만듦)

### 관리자 계정 설정 (set-admin)

관리자 계정을 처음 만들거나 비밀번호를 바꿀 때 서버에서 실행합니다. 비밀번호는 화면에 표시되지 않게 두 번 입력하고(20자 이상), bcrypt 해시만 저장됩니다.

```bash
cd ~/agent-room
.venv/bin/python room_gate.py set-admin <관리자 아이디>
systemctl --user restart agent-room-gate
```

비밀번호를 바꾸면 기존 관리자 로그인 세션은 모두 무효가 됩니다. bcrypt가 필요합니다(`VERSIONS.md`).

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
