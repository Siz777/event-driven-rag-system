"""rag.pipeline.answer (= rag.graph.run) — 검색·LLM·판정 fake 주입."""
import pytest

from events import InboundEvent
from rag.pipeline import NO_MATCH, SYSTEM, answer, build_context
from rag.retrieve import Hit

INB = InboundEvent(event_id="e1", source="cli", channel="terminal", user="me", text="펌프다운이 평소보다 오래 걸려요")
YES = lambda q, h: True   # noqa: E731


def hit(tid, score, cosine=None):
    return Hit(tid, score, {"_text": f"설비: PECVD\n증상: {tid} 증상", "root_cause": f"{tid} 원인", "symptom": f"{tid} 증상",
                            "equipment_id": "PECVD-01"}, cosine=score if cosine is None else cosine)


HITS = [hit("INC-2026-0012", 0.71), hit("INC-2026-0015", 0.66), hit("INC-2026-0100", 0.58)]


def test_answered_with_cited_citations_only():
    calls = {}

    def search(q, k):
        calls["search"] = (q, k)
        return HITS

    def chat(user, system):
        calls["chat"] = (user, system)
        return "(1) 유사 사례 [INC-2026-0012] [INC-2026-0015] [INC-2026-0012] (2) … (3) …"

    trace = {}
    out = answer(INB, search=search, chat=chat, grade=YES, k=3, trace=trace)
    assert calls["search"] == (INB.text, 3)
    assert out.status == "answered" and out.in_reply_to == "e1" and out.channel == "terminal"
    assert [c.ticket_id for c in out.citations] == ["INC-2026-0012", "INC-2026-0015"]   # 중복 제거, 미인용 0100 제외
    assert out.citations[0].score == 0.71
    user, system = calls["chat"]
    assert system == SYSTEM.format(k=3) and "상위 3건" in system
    assert "[질문]\n" + INB.text in user and "최종 원인: INC-2026-0012 원인" in user
    assert trace["gate"] == "pass" and trace["llm_calls"] == 4 and trace["kept"] == [h.ticket_id for h in HITS]


def test_no_hits_skips_llm():
    def boom(*a):
        raise AssertionError("LLM must not be called")

    out = answer(INB, search=lambda q, k: [], chat=boom, grade=boom)
    assert out.status == "no_match" and out.text == NO_MATCH and out.citations == []


def test_low_top_cosine_is_no_match_without_llm():
    def boom(*a):
        raise AssertionError("LLM must not be called")

    trace = {}
    out = answer(INB, search=lambda q, k: [hit("INC-2026-0001", 0.30)], chat=boom, grade=boom, min_score=0.50, trace=trace)
    assert out.status == "no_match" and trace["gate"] == "cosine" and trace["llm_calls"] == 0
    assert "참고(유사도 낮음): [INC-2026-0001]" in out.text and "신규 티켓" in out.text
    assert not out.text.startswith("유사 이력 없음")        # Slack 어댑터가 머리말을 붙인다


def test_cosine_gate_uses_max_vector_cosine_not_first_hit():
    """RRF 순서상 1위가 BM25 단독 문서(cosine None)여도 벡터 최고 cosine으로 판단."""
    hits = [Hit("INC-2026-0001", 0.03, HITS[0].payload, cosine=None), hit("INC-2026-0002", 0.03, cosine=0.7)]
    trace = {}
    answer(INB, search=lambda q, k: hits, chat=lambda u, s: "[INC-2026-0002]", grade=YES, trace=trace)
    assert trace["vector_top1"] == 0.7 and trace["gate"] == "pass"


def test_grade_filters_hits_and_all_no_is_no_match():
    seen = []

    def grade(q, h):
        seen.append(h.ticket_id)
        return h.ticket_id in {"INC-2026-0015", "INC-2026-0100"}

    calls = {}

    def chat(user, system):
        calls["user"] = user
        return "[INC-2026-0015] 참고"

    trace = {}
    out = answer(INB, search=lambda q, k: HITS, chat=chat, grade=grade, trace=trace)
    assert sorted(seen) == sorted(h.ticket_id for h in HITS) and trace["kept"] == ["INC-2026-0015", "INC-2026-0100"]
    assert "INC-2026-0012" not in calls["user"] and out.status == "answered"

    trace = {}
    out = answer(INB, search=lambda q, k: HITS, chat=chat, grade=lambda q, h: False, trace=trace)
    assert out.status == "no_match" and trace["gate"] == "llm" and trace["llm_calls"] == 3
    assert "[INC-2026-0012]" in out.text                      # 가장 가까운 티켓 참고


def test_single_yes_among_many_is_not_enough():
    """5건 중 1건만 yes → no_match (MIN_YES=2). 2건 yes면 통과."""
    def one_yes(q, h):
        return h.ticket_id == "INC-2026-0012"

    trace = {}
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "[INC-2026-0012]", grade=one_yes, trace=trace)
    assert out.status == "no_match" and trace["gate"] == "llm" and trace["graded"] == [True, False, False]
    two_yes = lambda q, h: h.ticket_id in {"INC-2026-0012", "INC-2026-0015"}   # noqa: E731
    assert answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "[INC-2026-0012]", grade=two_yes).status == "answered"
    # 검색 결과가 1건뿐이면 그 1건 yes로 통과
    assert answer(INB, search=lambda q, k: HITS[:1], chat=lambda u, s: "[INC-2026-0012]", grade=one_yes).status == "answered"


def test_llm_says_no_match_without_citation():
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "검색된 상위 3건 기준 유사 이력 없음.", grade=YES)
    assert out.status == "no_match" and out.citations == []


def test_grouped_citations_in_one_bracket():
    out = answer(INB, search=lambda q, k: HITS, grade=YES,
                 chat=lambda u, s: "원인 [INC-2026-0015, INC-2026-0100] 조치 [INC-2026-0012]")
    assert [c.ticket_id for c in out.citations] == ["INC-2026-0015", "INC-2026-0100", "INC-2026-0012"]


def test_uncited_hallucinated_ids_become_no_match():
    trace = {}
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "[INC-9999-9999] 참고", grade=YES, trace=trace)
    assert out.status == "no_match" and out.citations == [] and trace["gate"] == "no_citation"


def test_llm_error_propagates():
    def chat(user, system):
        raise TimeoutError("llm timeout")

    with pytest.raises(TimeoutError):
        answer(INB, search=lambda q, k: HITS, chat=chat, grade=YES)


def test_grade_error_propagates():
    def grade(q, h):
        raise TimeoutError("grade timeout")

    with pytest.raises(TimeoutError):
        answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "x", grade=grade)


def test_build_context_format():
    ctx = build_context(HITS[:1])
    assert ctx.startswith("[INC-2026-0012] (유사도 0.71)\n설비: PECVD")
    assert build_context([Hit("INC-2026-0001", 3.2, HITS[0].payload)]).startswith("[INC-2026-0001]\n")   # BM25 단독: 유사도 생략


def test_batch_grade_mode_one_call_and_min_yes():
    calls = []

    def batch(q, hits):
        calls.append([h.ticket_id for h in hits])
        return [h.ticket_id in {"INC-2026-0012", "INC-2026-0100"} for h in hits]

    def boom(q, h):
        raise AssertionError("per-doc grade must not be called in batch mode")

    trace = {}
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "[INC-2026-0012]", grade=boom, grade_batch=batch,
                 grade_mode="batch", trace=trace)
    assert out.status == "answered" and calls == [[h.ticket_id for h in HITS]]
    assert trace["kept"] == ["INC-2026-0012", "INC-2026-0100"] and trace["llm_calls"] == 2   # 판정 1 + 생성 1
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "x", grade_batch=lambda q, hs: [True, False, False],
                 grade_mode="batch")
    assert out.status == "no_match"
    with pytest.raises(ValueError):
        answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "x", grade_mode="weird")
