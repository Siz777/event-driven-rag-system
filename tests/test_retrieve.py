"""RRF 병합 (검색 백엔드 없이)."""
from rag.retrieve import Hit, rrf


def _hits(ids):
    return [Hit(t, 1.0 - i * 0.1, {"ticket_id": t}) for i, t in enumerate(ids)]


def test_rrf_both_lists_top1_wins_and_score_is_sum():
    out = rrf([_hits(["A", "B", "C"]), _hits(["A", "D"])], k=5)
    assert [h.ticket_id for h in out][:1] == ["A"]
    assert abs(out[0].score - 2 / 61) < 1e-9
    assert {h.ticket_id for h in out} == {"A", "B", "C", "D"}


def test_rrf_doc_ranked_mid_in_both_beats_single_list_top1():
    # X는 양쪽 2위(2/62≈0.0323), Y는 한쪽 1위만(1/61≈0.0164)
    out = rrf([_hits(["Y", "X"]), _hits(["Z", "X"])], k=3)
    assert out[0].ticket_id == "X" and out[0].score > out[1].score


def test_rrf_respects_k():
    assert len(rrf([_hits(list("ABCDEFG"))], k=3)) == 3
