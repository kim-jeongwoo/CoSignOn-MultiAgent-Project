"""AI 3개(Claude·Gemini·DeepSeek) + 팀 2개 정의. build_teams.py(컴포넌트 JSON 생성)와 experiment.py(자동 비교)가 같이 쓴다."""
import os
from pathlib import Path

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat
from autogen_core.models import ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient

ROOM = Path.home() / "agent-room"
GEMINI_MODEL = "gemini-3.8-flash"  # Gemini CLI(구글 계정 로그인)로 부른다 — gemini_cli_relay(8092)
NVIDIA_MODEL = "deepseek-ai/deepseek-v4.1-flash"  # NVIDIA API 카탈로그 — nvidia_relay(8093)


def load_env():
    # 키는 환경변수로만 넘긴다. 클라이언트에 직접 넣으면 팀 JSON·Studio DB에 키가 저장된다.
    for line in (ROOM / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _info(family="unknown"):
    return ModelInfo(vision=False, function_calling=False, json_output=False, family=family, structured_output=False)


def claude_client(model="sonnet"):
    # 중계 서버가 name 필드로 화자를 붙이므로 이름 접두사는 넣지 않는다.
    return OpenAIChatCompletionClient(model=model, base_url="http://127.0.0.1:8090/v1", api_key="unused",
                                      model_info=_info(), temperature=0.7)


def gemini_client():
    # gemini_cli_relay(8092)가 공식 Gemini CLI(-p)를 부른다. 화자 이름은 name 필드로 붙인다.
    return OpenAIChatCompletionClient(model=GEMINI_MODEL, base_url="http://127.0.0.1:8092/v1", api_key="unused",
                                      model_info=_info())


def deepseek_client():
    # nvidia_relay(8093)가 키를 붙인다. 추론 과정은 중계에서 걸러낸다.
    return OpenAIChatCompletionClient(model=NVIDIA_MODEL, base_url="http://127.0.0.1:8093/v1", api_key="unused",
                                      model_info=_info(), add_name_prefixes=True, temperature=0.7, max_tokens=2000)


def jev_selector_client():
    # jev_selector(8094): 선택 프롬프트의 표시(@@ROLES@@ 등)를 읽어 Jev로 ① 질문 유형 ② 다음 발언자를 판단한다.
    return OpenAIChatCompletionClient(model="jev-selector", base_url="http://127.0.0.1:8094/v1", api_key="unused",
                                      model_info=_info())


# AutoGen은 화자 이름에 영문·숫자·_·-만 허용한다. 내부 이름은 영문, 대화에서 부르는 이름은 한국어.
NAMES = {"claude": "클로드", "gemini": "제미나이", "deepseek": "딥시크", "human": "사람"}

RULES = """[공통 발언 규칙]
- 참여자: 클로드(claude, 제안 담당), 제미나이(gemini, 검토 담당), 딥시크(deepseek, 정리 담당), 사람(human, 사용자).
  대화 기록에는 영문 이름이 보이지만, 발언에서는 한국어 이름으로 부른다.
- 모든 발언의 첫 줄은 정확히 이 형식이다: [받는 사람: 이름 또는 전체] [동의/반대/보충]
  예) [받는 사람: 제미나이] [반대]
- 한국어로, 5문장 이내로 말한다.
- 다른 참여자의 발언을 대신 쓰거나 이름표를 붙여 흉내 내지 않는다.
- 합의가 되면 정리 담당(딥시크)이 최종안을 쓰고 마지막 줄에 '합의완료'라고만 쓴다. 정리 담당이 아니면 '합의완료'라는 말을 쓰지 않는다."""

ROLES = {
    "claude": ("제안 담당", "질문이나 직전 의견을 보고 구체적인 안을 제안하고, 지적을 받으면 안을 고친다."),
    "gemini": ("검토 담당", "직전 제안의 약점을 근거와 함께 최소 1개 지적한다. 약점이 해결됐으면 동의한다."),
    "deepseek": ("정리 담당", "논의를 정리한다. 제안과 검토가 합의에 이르렀으면 최종안을 쓰고 마지막 줄에 '합의완료'를 쓴다. 아직 쟁점이 남았으면 무엇이 남았는지 정리한다."),
}


def make_agents(claude_model="sonnet"):
    clients = {"claude": claude_client(claude_model), "gemini": gemini_client(), "deepseek": deepseek_client()}
    agents = []
    for name, (role, duty) in ROLES.items():
        ko = NAMES[name]
        sys_msg = f"너는 '{ko}'({name})이고 역할은 {role}이다. {duty}\n\n{RULES}"
        agents.append(AssistantAgent(name=name, model_client=clients[name], system_message=sys_msg,
                                     description=f"{ko}: {role}. {duty}"))
    return agents


def termination():
    return TextMentionTermination("합의완료") | MaxMessageTermination(16)


# @@표시@@는 jev_selector가 역할·후보·대화 기록을 꺼내는 데 쓴다. 판단 기준(질문 유형별 규칙)은 jev_selector.RULES.
SELECTOR_PROMPT = """다음에 발언할 참여자를 고른다.
@@ROLES@@
{roles}
@@CANDIDATES@@
{participants}
@@HISTORY@@
{history}
@@END@@
위 후보 중 다음에 발언할 참여자 이름 하나만 답하라."""


def team_a(with_human=True, claude_model="sonnet"):
    parts = make_agents(claude_model)
    if with_human:
        parts.append(UserProxyAgent(name="human", description="사람: 사용자. 의견을 내거나 방향을 정해 준다."))
    return RoundRobinGroupChat(parts, termination_condition=termination())


def team_b(with_human=True, claude_model="sonnet"):
    parts = make_agents(claude_model)
    if with_human:
        parts.append(UserProxyAgent(name="human", description="사람: 사용자. 의견을 내거나 방향을 정해 준다."))
    return SelectorGroupChat(parts, model_client=jev_selector_client(), termination_condition=termination(),
                             selector_prompt=SELECTOR_PROMPT, allow_repeated_speaker=False)
