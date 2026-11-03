"""설비 되묻기(Step 9): 조건·문구·답 해석 + 그래프 분기(fake 검색·LLM)."""
from events import InboundEvent
from rag.clarify import answered_type, clarify_text, rewrite_followup, should_clarify
from rag.pipeline import answer
from rag.retrieve import Hit

Q = "챔버 진공이 계속 떨어지는데 어디부터 확인해야 해?"
INB = InboundEvent(event_id="e9", source="cli", channel="terminal", user="me", text=Q)


def hit(tid, etype, cosine, eid=None):
    return Hit(tid, cosine, {"_text": f"설비: {etype}\n증상: {tid}", "root_cause": f"{tid} 원인", "symptom": f"{tid} 증상",
                             "equipment_type": etype, "equipment_id": eid or f"{etype}-01"}, cosine=cosine)


MIXED = [hit("INC-2026-0129", "PVD", 0.62), hit("INC-2026-0001", "PECVD", 0.61), hit("INC-2026-0073", "ETCH", 0.60),
         hit("INC-2026-0002", "PECVD", 0.59), hit("INC-2026-0130", "PVD", 0.58)]
SAME = [hit("INC-2026-0001", "PECVD", 0.62), hit("INC-2026-0002", "PECVD", 0.61), hit("INC-2026-0129", "PVD", 0.50)]


def test_should_clarify_when_top2_differ_and_no_equipment_in_query():
    d = should_clarify(Q, MIXED)
    assert d is not None and d.types == ["PVD", "PECVD", "ETCH"] and d.reason == "top2_different_types"
    assert d.candidates[0].ticket_id == "INC-2026-0129" and d.candidates[1].equipment_id == "PECVD-01"


def test_no_clarify_when_query_names_equipment_or_code():
    assert should_clarify("PECVD-03 챔버 진공이 떨어져요", MIXED) is None
    assert should_clarify("E-1101 떴는데 진공이 떨어져요", MIXED) is None
    assert should_clarify("식각 장비 진공이 떨어져요", MIXED) is None            # 키워드만 있어도 묻지 않는다
    assert should_clarify(rewrite_followup(Q, "PECVD"), MIXED) is None         # 되묻기 답을 합친 2회째 질의


def test_no_clarify_when_single_type_or_clear_leader():
    assert should_clarify(Q, [hit("INC-2026-0001", "PECVD", 0.6), hit("INC-2026-0002", "PECVD", 0.5)]) is None
    assert should_clarify(Q, SAME) is None                                     # 1·2위 PECVD, 다른 설비 최상위는 0.12 뒤
    close = [hit("INC-2026-0001", "PECVD", 0.62), hit("INC-2026-0002", "PECVD", 0.615), hit("INC-2026-0129", "PVD", 0.60)]
    assert should_clarify(Q, close).reason == "other_type_close"               # 다른 설비 최상위가 0.02 뒤면 묻는다
    assert should_clarify(Q, MIXED[:1]) is None                                # 결과 1건


def test_no_clarify_below_cosine_gate():
    low = [hit("INC-2026-0129", "PVD", 0.45), hit("INC-2026-0001", "PECVD", 0.44)]
    assert should_clarify(Q, low, min_cosine=0.50) is None
    assert should_clarify(Q, low) is not None


def test_clarify_text_lists_candidates_and_how_to_answer():
    d = should_clarify(Q, MIXED)
    s = clarify_text(d)
    assert s.startswith("어느 설비에서 난 증상인가요?") and "PVD(스퍼터)" in s and "[INC-2026-0001] PECVD-01" in s
    assert "PVD / PECVD / ETCH" in s and "PVD-03" in s and not s.startswith("유사 이력 없음")
    assert s.count("•") == 3


def test_answered_type_accepts_name_id_keyword_rejects_ambiguous():
    assert answered_type("PECVD") == "PECVD"
    assert answered_type("pecvd-03 이에요") == "PECVD"
    assert answered_type("CVD 장비요") == "PECVD"
    assert answered_type("식각") == "ETCH"
    assert answered_type("모르겠어요") is None
    assert answered_type("PECVD 아니면 PVD") is None
    assert rewrite_followup(Q, "PVD") == Q + " (설비: PVD)"


def test_graph_asks_after_grade_passes_with_mixed_types():
    calls = []
    trace = {}
    out = answer(INB, search=lambda q, k: MIXED, chat=lambda u, s: calls.append(u) or "x", grade=lambda q, h: True, trace=trace)
    assert out.status == "clarify" and out.citations == [] and out.text.startswith("어느 설비에서")
    assert calls == [] and trace["llm_calls"] == 5 and trace["candidates"] == ["PVD", "PECVD", "ETCH"]


def test_graph_does_not_ask_when_grade_leaves_one_type_or_negative():
    # 판정이 PECVD만 남기면 묻지 않고 답한다
    out = answer(INB, search=lambda q, k: MIXED, chat=lambda u, s: "[INC-2026-0001] [INC-2026-0002]",
                 grade=lambda q, h: h.payload["equipment_type"] == "PECVD")
    assert out.status == "answered" and [c.ticket_id for c in out.citations] == ["INC-2026-0001", "INC-2026-0002"]
    # negative(판정 전부 no)는 되묻기가 아니라 no_match
    out = answer(INB, search=lambda q, k: MIXED, chat=lambda u, s: "x", grade=lambda q, h: False)
    assert out.status == "no_match"


def test_graph_clarify_can_be_disabled_or_consumed():
    out = answer(INB, search=lambda q, k: MIXED, chat=lambda u, s: "[INC-2026-0129]", grade=lambda q, h: True, clarify=False)
    assert out.status == "answered"
    out = answer(INB, search=lambda q, k: MIXED, chat=lambda u, s: "[INC-2026-0129]", grade=lambda q, h: True, allow_clarify=False)
    assert out.status == "answered"                                            # 2회째 호출: 묻지 않는다
    inb2 = INB.model_copy(update={"text": rewrite_followup(Q, "PVD")})
    seen = {}
    out = answer(inb2, search=lambda q, k: seen.setdefault("q", q) and MIXED, chat=lambda u, s: "[INC-2026-0129]", grade=lambda q, h: True)
    assert out.status == "answered" and seen["q"].endswith("(설비: PVD)")
