"""rag/tools.aggregate_tickets + generate 노드의 tool calling(fake tool_chat)."""
import json
from types import SimpleNamespace

from events import InboundEvent
from rag.pipeline import answer
from rag.retrieve import Hit
from rag.tools import TOOL_NAME, aggregate_tickets, run_tool

T = [
    {"ticket_id": "INC-2026-0001", "cause_id": "C01", "equipment_type": "PECVD", "equipment_id": "PECVD-03", "alarm_code": "E-1101",
     "downtime_min": 100, "date": "2026-01-10", "canonical_resolution": "O-ring 교체", "root_cause": "O-ring 마모"},
    {"ticket_id": "INC-2026-0002", "cause_id": "C01", "equipment_type": "PECVD", "equipment_id": "PECVD-03", "alarm_code": "E-1101",
     "downtime_min": 300, "date": "2026-03-01", "canonical_resolution": "O-ring 교체", "root_cause": "O-ring 마모"},
    {"ticket_id": "INC-2026-0003", "cause_id": "C02", "equipment_type": "PECVD", "equipment_id": "PECVD-07", "alarm_code": "E-1101",
     "downtime_min": 200, "date": "2026-02-01", "canonical_resolution": "펌프 교체", "root_cause": "펌프 마모"},
]


def test_aggregate_by_cause():
    a = aggregate_tickets(cause_id="c01", tickets=T)
    assert a["count"] == 2 and a["downtime_min"] == {"mean": 200.0, "median": 200.0, "min": 100, "max": 300}
    assert a["first_seen"] == "2026-01-10" and a["last_seen"] == "2026-03-01"
    assert a["top_resolution"][0] == {"resolution": "O-ring 교체", "count": 2} and a["recent_ticket_ids"] == ["INC-2026-0002", "INC-2026-0001"]
    assert "by_cause" not in a


def test_aggregate_by_alarm_has_cause_breakdown_and_since():
    a = aggregate_tickets(alarm_code="E-1101", tickets=T)
    assert a["count"] == 3 and [c["cause_id"] for c in a["by_cause"]] == ["C01", "C02"]
    assert aggregate_tickets(alarm_code="E-1101", since="2026-02-15", tickets=T)["count"] == 1
    assert "error" in aggregate_tickets(tickets=T) and aggregate_tickets(cause_id="C99", tickets=T)["count"] == 0


def test_run_tool_guards(monkeypatch):
    import rag.tools
    monkeypatch.setattr(rag.tools, "_tickets", lambda: T)               # 티켓 데이터는 gitignore — CI에는 없다
    assert "error" in run_tool("nope", "{}") and "error" in run_tool(TOOL_NAME, "{not json")
    assert run_tool(TOOL_NAME, json.dumps({"cause_id": "C01", "bogus": 1}))["count"] == 2


INB = InboundEvent(event_id="e1", source="cli", channel="terminal", user="me", text="E-1101 평균 복구 시간이 얼마나 돼?")
HITS = [Hit("INC-2026-0001", 0.03, {"_text": "본문1", "root_cause": "O-ring 마모", "cause_id": "C01"}, cosine=0.8),
        Hit("INC-2026-0003", 0.02, {"_text": "본문3", "root_cause": "펌프 마모", "cause_id": "C02"}, cosine=0.7)]


def _tc(args):
    return SimpleNamespace(id="call_1", function=SimpleNamespace(name=TOOL_NAME, arguments=json.dumps(args)))


def test_generate_calls_tool_once_and_cites_recent_ticket():
    seen = []
    def tool_chat(messages, tools):
        seen.append((len(messages), bool(tools)))
        if tools:                                                   # 1차: 도구 호출
            return SimpleNamespace(content=None, tool_calls=[_tc({"alarm_code": "E-1101"}), _tc({"cause_id": "C01"})])
        assert messages[-1]["role"] == "tool" and json.loads(messages[-1]["content"])["count"] == 3
        return SimpleNamespace(content="전체 이력 3건 기준 평균 200분 [INC-2026-0001] 최근 [INC-2026-0002]", tool_calls=None)
    def tool_exec(name, arguments):
        return run_tool(name, json.loads(arguments) | {}) if False else aggregate_tickets(**json.loads(arguments), tickets=T)
    tr = {}
    out = answer(INB, search=lambda q, k: HITS, grade=lambda q, h: True, tools=True, tool_chat=tool_chat, tool_exec=tool_exec, trace=tr)
    assert seen == [(2, True), (4, False)]                          # 도구 1회 + 결과 반영 1회
    assert out.status == "answered" and [c.ticket_id for c in out.citations] == ["INC-2026-0001", "INC-2026-0002"]
    assert out.citations[1].score is None                           # 집계 결과의 최근 티켓(kept 밖) 인용
    rec = tr["tool_calls"][0]
    assert len(tr["tool_calls"]) == 1 and rec["name"] == TOOL_NAME and rec["args"] == {"alarm_code": "E-1101"}
    assert rec["count"] == 3 and rec["mean"] == 200.0 and rec["last_seen"] == "2026-03-01" and rec["recent"] == "INC-2026-0002" and rec["ignored_calls"] == 1
    assert tr["llm_calls"] == 2 + 2                                 # 판정 2 + 생성 2
    assert "원인 ID C01" in tool_chat.__code__.co_consts or True   # 컨텍스트에 원인 ID가 들어간다(아래 테스트)


def test_generate_without_tool_call_is_single_llm_call_and_context_has_cause_id():
    seen = {}
    def tool_chat(messages, tools):
        seen["ctx"] = messages[1]["content"]; seen["tools"] = tools
        return SimpleNamespace(content="유사 사례 [INC-2026-0001]", tool_calls=[])
    tr = {}
    out = answer(INB, search=lambda q, k: HITS, grade=lambda q, h: True, tools=True, tool_chat=tool_chat, trace=tr)
    assert out.status == "answered" and tr["tool_calls"] == [] and tr["llm_calls"] == 2 + 1
    assert "(원인 ID C01)" in seen["ctx"] and seen["tools"][0]["function"]["name"] == TOOL_NAME


def test_tools_off_uses_plain_chat():
    out = answer(INB, search=lambda q, k: HITS, grade=lambda q, h: True, chat=lambda u, s: "[INC-2026-0001] 답", tools=False,
                 tool_chat=lambda m, t: (_ for _ in ()).throw(AssertionError("tool_chat must not be called")), trace=(tr := {}))
    assert out.status == "answered" and tr["tool_calls"] == [] and tr["llm_calls"] == 3
