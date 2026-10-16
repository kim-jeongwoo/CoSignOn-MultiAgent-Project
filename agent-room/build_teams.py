"""팀 A·B를 AutoGen 컴포넌트 JSON으로 저장한다 (Studio 가져오기용).

키는 모두 중계 서비스(8090~8094) 환경에만 있고, 팀 JSON의 api_key는 자리 값("unused")뿐이다.
"""
import json

from teams import ROOM, load_env, team_a, team_b

load_env()
LABELS = {
    "team_a": ("팀 A 순서 고정", "RoundRobin: 클로드 → 제미나이 → 딥시크 → 사람"),
    "team_b": ("팀 B AI가 다음 발언자 선택", "Selector: Jev가 질문 유형(정답형/판단형)과 대화를 보고 다음 발언자를 고름"),
}


for key, fn in (("team_a", team_a), ("team_b", team_b)):
    comp = json.loads(fn().dump_component().model_dump_json())
    comp["label"], comp["description"] = LABELS[key]
    out = ROOM / "teams" / f"{key}.json"
    out.write_text(json.dumps(comp, ensure_ascii=False, indent=2))
    print(out, len(out.read_text()))
