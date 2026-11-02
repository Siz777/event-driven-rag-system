"""GitHub 이슈 도메인 평가 (3주차 Step 6). DOMAIN=github_issues 로 실행.

DOMAIN=github_issues python -m scripts.eval_github [--methods vector,bm25,hybrid] [--k 5] [--boot 2000]
- 시간 누수 차단: 질의 이슈보다 나중에 만들어진 문서는 후보에서 뺀다(top-20 검색 → created_at 필터 → top-5).
- 지표: R@1·R@5·MRR + 부트스트랩 95% 신뢰구간(행 재표집, 기본 2000회). 결과는 experiments.jsonl에 한 줄씩.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time

from rag import config
from rag.documents import load_jsonl
from rag.eval import mrr, recall_at_k
from rag.retrieve import search

assert config.DOMAIN == "github_issues", "DOMAIN=github_issues 로 실행"


def boot_ci(xs: list[float], n: int, rng: random.Random) -> tuple[float, float]:
    means = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3)


def run(method: str, k: int, boot: int, rows: list[dict], created: dict[str, str]) -> dict:
    r1, r5, rr = [], [], []
    failures = []
    for r in rows:
        hits = search(r["query"], k=4 * k, method=method)
        hits = [h for h in hits if created.get(h.ticket_id, "") < r["created_at"]][:k]   # 시간 누수 차단
        ranked = [h.ticket_id for h in hits]
        rel = set(r["relevant_ticket_ids"])
        r1.append(recall_at_k(ranked, rel, 1)); r5.append(recall_at_k(ranked, rel, k)); rr.append(mrr(ranked, r["source_ticket_id"], rel))
        if r5[-1] == 0.0:
            failures.append({"dup": r.get("dup_number"), "query": r["query"].split("\n")[0][:90], "expected": sorted(rel), "got": ranked[:3]})
    rng = random.Random(0)
    rep = {"method": method, "k": k, "n": len(rows), "domain": config.DOMAIN, "at": time.strftime("%Y-%m-%dT%H:%M"),
           "recall@1": round(statistics.mean(r1), 3), f"recall@{k}": round(statistics.mean(r5), 3), "mrr": round(statistics.mean(rr), 3),
           "ci95_recall@1": boot_ci(r1, boot, rng), f"ci95_recall@{k}": boot_ci(r5, boot, rng), "ci95_mrr": boot_ci(rr, boot, rng),
           "failures": failures, "embed_model": config.EMBED_MODEL, "collection": config.COLLECTION}
    with open(config.EVAL_DIR / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({**rep, "failures": len(failures)}, ensure_ascii=False) + "\n")
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="vector,bm25,hybrid")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--min-earlier", type=int, default=200,
                    help="질의 이슈보다 먼저 만들어진 코퍼스 문서가 이보다 적은 질의는 제외(코퍼스가 최신 2000건이라 옛 질의는 후보가 없다)")
    args = ap.parse_args()
    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    created = {t["ticket_id"]: t["created_at"] for t in load_jsonl(config.DATA_DIR / "tickets.jsonl")}
    dates = sorted(created.values())
    import bisect
    before = len(rows)
    rows = [r for r in rows if bisect.bisect_left(dates, r["created_at"]) >= args.min_earlier]
    print(f"queries: {before} → {len(rows)} (earlier-docs ≥ {args.min_earlier})")
    reps = [run(m, args.k, args.boot, rows, created) for m in args.methods.split(",")]
    print(f"| method | n | R@1 [95% CI] | R@{args.k} [95% CI] | MRR [95% CI] | 실패 |")
    print("|---|---|---|---|---|---|")
    for r in reps:
        print(f"| {r['method']} | {r['n']} | {r['recall@1']} {list(r['ci95_recall@1'])} | {r[f'recall@{args.k}']} {list(r[f'ci95_recall@{args.k}'])} "
              f"| {r['mrr']} {list(r['ci95_mrr'])} | {len(r['failures'])} |")
    for r in reps:
        print(f"\n## {r['method']} 실패 {len(r['failures'])}건")
        for f in r["failures"][:8]:
            print(f"- #{f['dup']} {f['query']} → expected {f['expected']}, got {f['got']}")


if __name__ == "__main__":
    main()
