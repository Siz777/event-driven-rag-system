"""검색 품질 평가. Recall@K, MRR, 난이도별 분해, 실패 사례 덤프.
평가셋 형식(eval.jsonl): {query, relevant_ticket_ids, source_ticket_id, difficulty, source}
difficulty: exact | paraphrase | multi_hop | human | negative (negative는 relevant 비어 있음)
source: llm(기본, build_evalset) | human | claude_chat_draft(1주차 19) | chatgpt_draft(11-03 108) — append_human_queries.
같은 LLM이 쓴 글끼리 매칭되는 편향 확인용으로 출처별 따로 집계.
MRR: source_ticket_id가 있으면 그 티켓의 순위, 없으면(사람 질의) relevant 중 첫 등장 순위.
"""
import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime

from . import config
from .documents import load_jsonl
from .retrieve import search


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if relevant & set(ranked[:k]) else 0.0


def mrr(ranked: list[str], target: str, relevant: set[str] | None = None) -> float:
    """target(출처 티켓)의 역순위. target이 없으면 relevant 중 첫 등장의 역순위."""
    want = {target} if target else (relevant or set())
    for i, t in enumerate(ranked, 1):
        if t in want:
            return 1.0 / i
    return 0.0


def run(method: str = "vector", k: int = 5, dump_failures: bool = True) -> dict:
    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    by_diff: dict[str, list[dict]] = defaultdict(list)
    by_source: dict[str, list[dict]] = defaultdict(list)
    failures = []
    neg_top1 = []
    filter_hits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))   # applied → difficulty → n
    traces = []

    for r in rows:
        trace: dict = {}
        hits = search(r["query"], k=k, method=method, trace=trace)
        ranked = [h.ticket_id for h in hits]
        if trace:
            filter_hits[trace["applied"]][r["difficulty"]] += 1
            traces.append({"query": r["query"], "difficulty": r["difficulty"], **trace, "top": ranked[:3]})
        if r["difficulty"] == "negative":
            neg_top1.append(hits[0].score if hits else 0.0)
            continue
        rel = set(r["relevant_ticket_ids"])
        rec = recall_at_k(ranked, rel, k)
        rr = mrr(ranked, r.get("source_ticket_id", ""), rel)
        m = {"recall": rec, "mrr": rr, "r1": recall_at_k(ranked, rel, 1), "r3": recall_at_k(ranked, rel, 3)}
        by_diff[r["difficulty"]].append(m)
        by_source[r.get("source", "llm")].append(m)
        if rec == 0.0:
            failures.append({"query": r["query"], "difficulty": r["difficulty"], "source": r.get("source", "llm"),
                             "expected": sorted(rel)[:3], "got": [(h.ticket_id, round(h.score, 3)) for h in hits]})

    def agg(xs: list[dict]) -> dict:
        return {"n": len(xs), "recall@1": round(statistics.mean(x["r1"] for x in xs), 3),
                "recall@3": round(statistics.mean(x["r3"] for x in xs), 3),
                f"recall@{k}": round(statistics.mean(x["recall"] for x in xs), 3),
                "mrr": round(statistics.mean(x["mrr"] for x in xs), 3)}

    report = {"method": method, "k": k, "doc_text_mode": config.DOC_TEXT_MODE, "n": len(rows),
              "at": datetime.now().isoformat(timespec="minutes")}
    all_m = []
    for d, xs in sorted(by_diff.items()):
        report[d] = agg(xs)
        all_m += xs
    report["overall"] = agg(all_m)
    report["by_source"] = {src: agg(xs) for src, xs in sorted(by_source.items())}
    if neg_top1:
        # 3주차 negative 임계값 캘리브레이션에 쓰는 통계
        report["negative"] = {"n": len(neg_top1), "top1_score_mean": round(statistics.mean(neg_top1), 3),
                              "top1_score_max": round(max(neg_top1), 3)}

    if traces:
        report["filter_hits"] = {a: {**dict(d), "total": sum(d.values())} for a, d in sorted(filter_hits.items())}
        with open(config.EVAL_DIR / f"filter_trace_{method}.jsonl", "w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    if dump_failures:
        out = config.EVAL_DIR / (f"failures_{method}.md" if config.DOC_TEXT_MODE == "full"
                                 else f"failures_{method}_{config.DOC_TEXT_MODE}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# 실패 사례 ({method}, doc_text={config.DOC_TEXT_MODE}, {report['at']})\n\n")
            for x in failures:
                f.write(f"## [{x['difficulty']}/{x['source']}] {x['query']}\n- expected: {x['expected']}\n- got: {x['got']}\n\n")
        report["failures_file"] = str(out)

    # 실험 로그에 한 줄 append — 재현 정보(모델·컬렉션)까지
    with open(config.EVAL_DIR / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({**report, "embed_model": config.EMBED_MODEL, "collection": config.COLLECTION},
                           ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", default="vector")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    print(json.dumps(run(args.method, args.k), ensure_ascii=False, indent=2))
