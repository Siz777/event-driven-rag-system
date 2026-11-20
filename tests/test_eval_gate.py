"""scripts/eval_gate — 기록값 해석·manifest 검증(LLM·모델 없음)."""
import json

from scripts.eval_gate import THRESHOLDS, _ok, check_manifest, recorded_graph, recorded_retrieval, weighted_r5


def test_weighted_r5_uses_170_basis_sources_only():
    bs = {"llm": {"n": 120, "recall@5": 0.892}, "claude_chat_draft": {"n": 19, "recall@5": 0.842},
          "chatgpt_draft": {"n": 95, "recall@5": 0.3}, "human": {"n": 10, "recall@5": 0.1}}
    assert weighted_r5(bs) == 0.885 and weighted_r5({"human": {"n": 10, "recall@5": 0.1}}) is None


def test_recorded_picks_latest_matching_rows():
    rows = [{"method": "vector", "by_source": {"llm": {"n": 1, "recall@5": 0.5}}},
            {"method": "vector", "by_source": {"llm": {"n": 1, "recall@5": 0.9}}},
            {"method": "vector", "eval_file": "eval_noise.jsonl", "by_source": {"llm": {"n": 1, "recall@5": 0.1}}},
            {"method": "graph(hybrid+filter+grade:per_doc)", "split": "val", "graph": {"negative": {"rate": 0.875}, "positive": {"rate": 0.0}}},
            {"method": "graph(hybrid+filter+grade:per_doc)", "split": "all", "graph": {"negative": {"rate": 0.1}, "positive": {"rate": 0.9}}}]
    r = recorded_retrieval(rows)
    assert r["vector.recall@5"] == 0.9 and r["hybrid+filter.recall@5"] is None
    assert recorded_graph(rows) == {"graph.negative_no_match": 0.875, "graph.positive_false_no_match": 0.0}


def test_manifest_checks(tmp_path):
    good = {"name": "x", "source_model": "m", "threshold": 0.0047, "max_length": 256, "band": [0.0047, 0.9993],
            "val": {"neg_no_match": 1.0, "false_no_match": 0.07}}
    p = tmp_path / "m.json"; p.write_text(json.dumps(good))
    vals, problems = check_manifest(p)
    assert problems == [] and vals == {"reranker.val.neg_no_match": 1.0, "reranker.val.false_no_match": 0.07}
    bad = {**good, "threshold": 1.5, "band": [0.9, 0.1]}
    p.write_text(json.dumps(bad))
    _, problems = check_manifest(p)
    assert any("threshold" in x for x in problems) and any("band" in x for x in problems)
    assert check_manifest(tmp_path / "nope.json")[1] == [f"manifest missing: {tmp_path / 'nope.json'}"]


def test_thresholds_are_current_minus_0_02():
    assert THRESHOLDS["vector.recall@5"] == ("ge", 0.865) and THRESHOLDS["graph.positive_false_no_match"] == ("le", 0.02)
    assert _ok("ge", 0.865, 0.865) and not _ok("ge", 0.864, 0.865) and _ok("le", 0.02, 0.02) and not _ok("le", 0.021, 0.02)


def test_recorded_retrieval_ignores_other_embedding_models():
    # Step 12 임베딩 비교 행이 최신이어도 게이트는 기준 모델(bge-m3) 행을 읽는다. embed_model 없는 옛 행은 기준으로 본다
    rows = [{"method": "vector", "by_source": {"llm": {"n": 1, "recall@5": 0.9}}},
            {"method": "vector", "embed_model": "BAAI/bge-m3", "by_source": {"llm": {"n": 1, "recall@5": 0.8}}},
            {"method": "vector", "embed_model": "Qwen/Qwen3-Embedding-0.6B", "by_source": {"llm": {"n": 1, "recall@5": 0.1}}}]
    assert recorded_retrieval(rows)["vector.recall@5"] == 0.8
