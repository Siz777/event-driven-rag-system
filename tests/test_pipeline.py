"""rag.pipeline.answer — 검색·LLM fake 주입."""
import pytest

from events import InboundEvent
from rag.pipeline import NO_MATCH, SYSTEM, answer, build_context
from rag.retrieve import Hit

INB = InboundEvent(event_id="e1", source="cli", channel="terminal", user="me", text="펌프다운이 평소보다 오래 걸려요")


def hit(tid, score):
    return Hit(tid, score, {"_text": f"설비: PECVD\n증상: {tid} 증상", "root_cause": f"{tid} 원인"})


HITS = [hit("INC-2026-0012", 0.71), hit("INC-2026-0015", 0.66), hit("INC-2026-0100", 0.58)]


def test_answered_with_cited_citations_only():
    calls = {}

    def search(q, k):
        calls["search"] = (q, k)
        return HITS

    def chat(user, system):
        calls["chat"] = (user, system)
        return "(1) 유사 사례 [INC-2026-0012] [INC-2026-0015] [INC-2026-0012] (2) … (3) …"

    out = answer(INB, search=search, chat=chat, k=3)
    assert calls["search"] == (INB.text, 3)
    assert out.status == "answered" and out.in_reply_to == "e1" and out.channel == "terminal"
    assert [c.ticket_id for c in out.citations] == ["INC-2026-0012", "INC-2026-0015"]   # 중복 제거, 미인용 0100 제외
    assert out.citations[0].score == 0.71
    user, system = calls["chat"]
    assert system == SYSTEM.format(k=3) and "상위 3건" in system
    assert "[질문]\n" + INB.text in user and "최종 원인: INC-2026-0012 원인" in user


def test_no_hits_skips_llm():
    def chat(user, system):
        raise AssertionError("LLM must not be called")

    out = answer(INB, search=lambda q, k: [], chat=chat)
    assert out.status == "no_match" and out.text == NO_MATCH and out.citations == []


def test_low_top_score_is_no_match():
    def chat(user, system):
        raise AssertionError("LLM must not be called")

    out = answer(INB, search=lambda q, k: [hit("INC-2026-0001", 0.30)], chat=chat, min_score=0.45)
    assert out.status == "no_match"


def test_llm_says_no_match_without_citation():
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "검색된 상위 3건 기준 유사 이력 없음.")
    assert out.status == "no_match" and out.citations == []


def test_grouped_citations_in_one_bracket():
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "원인 [INC-2026-0015, INC-2026-0100] 조치 [INC-2026-0012]")
    assert [c.ticket_id for c in out.citations] == ["INC-2026-0015", "INC-2026-0100", "INC-2026-0012"]


def test_uncited_hallucinated_ids_are_dropped():
    out = answer(INB, search=lambda q, k: HITS, chat=lambda u, s: "[INC-9999-9999] 참고")
    assert out.status == "answered" and out.citations == []


def test_llm_error_propagates():
    def chat(user, system):
        raise TimeoutError("llm timeout")

    with pytest.raises(TimeoutError):
        answer(INB, search=lambda q, k: HITS, chat=chat)


def test_build_context_format():
    ctx = build_context(HITS[:1])
    assert ctx.startswith("[INC-2026-0012] (유사도 0.71)\n설비: PECVD")
