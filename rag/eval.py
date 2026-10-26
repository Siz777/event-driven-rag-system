"""검색 품질 평가. Recall@K, MRR, 난이도별 분해, 실패 사례 덤프.
평가셋 형식(eval.jsonl): {query, relevant_ticket_ids, source_ticket_id, difficulty}
difficulty: exact | paraphrase | multi_hop | negative (negative는 relevant 비어 있음)
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


def mrr(ranked: list[str], target: str) -> float:
    for i, t in enumerate(ranked, 1):
        if t == target:
            return 1.0 / i
    return 0.0


def run(method: str = "vector", k: int = 5, dump_failures: bool = True) -> dict:
    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    by_diff: dict[str, list[dict]] = defaultdict(list)
    failures = []
    neg_top1 = []

    for r in rows:
        hits = search(r["query"], k=k, method=method)
        ranked = [h.ticket_id for h in hits]
        if r["difficulty"] == "negative":
            neg_top1.append(hits[0].score if hits else 0.0)
            continue
        rel = set(r["relevant_ticket_ids"])
        rec = recall_at_k(ranked, rel, k)
        rr = mrr(ranked, r.get("source_ticket_id", ""))
        by_diff[r["difficulty"]].append({"recall": rec, "mrr": rr})
        if rec == 0.0:
            failures.append({"query": r["query"], "difficulty": r["difficulty"],
                             "expected": sorted(rel)[:3], "got": [(h.ticket_id, round(h.score, 3)) for h in hits]})

    report = {"method": method, "k": k, "n": len(rows), "at": datetime.now().isoformat(timespec="minutes")}
    all_rec, all_rr = [], []
    for d, xs in sorted(by_diff.items()):
        rec = statistics.mean(x["recall"] for x in xs)
        rr = statistics.mean(x["mrr"] for x in xs)
        report[d] = {"n": len(xs), f"recall@{k}": round(rec, 3), "mrr": round(rr, 3)}
        all_rec += [x["recall"] for x in xs]
        all_rr += [x["mrr"] for x in xs]
    report["overall"] = {"n": len(all_rec), f"recall@{k}": round(statistics.mean(all_rec), 3),
                         "mrr": round(statistics.mean(all_rr), 3)}
    if neg_top1:
        # 3주차 negative 임계값 캘리브레이션에 쓰는 통계
        report["negative"] = {"n": len(neg_top1), "top1_score_mean": round(statistics.mean(neg_top1), 3),
                              "top1_score_max": round(max(neg_top1), 3)}

    if dump_failures:
        out = config.EVAL_DIR / f"failures_{method}.md"
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# 실패 사례 ({method}, {report['at']})\n\n")
            for x in failures:
                f.write(f"## [{x['difficulty']}] {x['query']}\n- expected: {x['expected']}\n- got: {x['got']}\n\n")
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
