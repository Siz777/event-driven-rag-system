"""노이즈 강건성 평가 (3주차 Step 7). eval_noise.jsonl(79행 × typo/abbr/both) + 깨끗한 79행.

python -m scripts.eval_noise                       # vector · hybrid+filter, 현재 토크나이저 스위치(BM25_DICT/BM25_CODES)로
BM25_DICT=0  python -m scripts.eval_noise --methods hybrid+filter   # 사전 끄고 (먼저 BM25_DICT=0 python -m rag.bm25_index)
BM25_CODES=0 python -m scripts.eval_noise --methods hybrid+filter   # 코드 정규식 끄고
python -m scripts.eval_noise --graph              # graph_results.jsonl(깨끗) + graph_results_noise.jsonl 집계(LLM 호출 없음)
결과는 EVAL_DIR/noise_results.jsonl 에 한 줄씩(변형 × method × 스위치), --table 로 표 출력.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict

from rag import config
from rag.documents import load_jsonl
from rag.eval import mrr, recall_at_k
from rag.retrieve import search
from rag.tokenize import USE_CODES, USE_DICT

OUT = config.EVAL_DIR / "noise_results.jsonl"
SETS = ["clean", "typo", "abbr", "both", "typo10", "typo30", "both30"]


def rows_by_set() -> dict[str, list[dict]]:
    noise = load_jsonl(config.EVAL_DIR / "eval_noise.jsonl")
    by = defaultdict(list)
    for r in noise:
        by[r["noise"]].append(r)
    by["clean"] = [{**r, "query": r["clean_query"], "changed": True} for r in by["typo"]]
    for s in SETS:
        by.setdefault(s, [])
    return by


def agg(items: list[tuple[dict, list[str]]], k: int) -> dict:
    r1 = [recall_at_k(ranked, set(r["relevant_ticket_ids"]), 1) for r, ranked in items]
    r5 = [recall_at_k(ranked, set(r["relevant_ticket_ids"]), k) for r, ranked in items]
    rr = [mrr(ranked, r.get("source_ticket_id", ""), set(r["relevant_ticket_ids"])) for r, ranked in items]
    return {"n": len(items), "recall@1": round(statistics.mean(r1), 3), f"recall@{k}": round(statistics.mean(r5), 3), "mrr": round(statistics.mean(rr), 3)}


def run(method: str, k: int) -> None:
    by = rows_by_set()
    tag = f"dict={int(USE_DICT)},codes={int(USE_CODES)}"
    with open(OUT, "a", encoding="utf-8") as f:
        for s in SETS:
            items = [(r, [h.ticket_id for h in search(r["query"], k=k, method=method)]) for r in by[s]]
            rep = {"method": method, "switch": tag, "set": s, **agg(items, k),
                   "changed_only": agg([x for x in items if x[0]["changed"]], k),
                   "by_difficulty": {d: agg([x for x in items if x[0]["difficulty"] == d], k) for d in ("exact", "paraphrase", "human")}}
            f.write(json.dumps(rep, ensure_ascii=False) + "\n")
            print(f"{method:<14} {tag:<16} {s:<6} R@1 {rep['recall@1']}  R@5 {rep[f'recall@{k}']}  MRR {rep['mrr']}  (changed n={rep['changed_only']['n']}: R@5 {rep['changed_only'][f'recall@{k}']})")


def graph_summary(k: int) -> None:
    clean = {r["query"]: r for r in load_jsonl(config.EVAL_DIR / "graph_results.jsonl")}
    noise = load_jsonl(config.EVAL_DIR / "graph_results_noise.jsonl")
    by = rows_by_set()
    with open(OUT, "a", encoding="utf-8") as f:
        for s in SETS:
            if s == "clean":
                res = [clean[r["query"]] for r in by["clean"] if r["query"] in clean]
            else:
                res = [x for x in noise if x.get("noise") == s]
            if not res:
                continue
            res = [r for r in res if r["status"] != "error"]
            items = [({**r, "relevant_ticket_ids": r["relevant"]}, r.get("kept") or []) for r in res]
            rep = {"method": "graph", "switch": "per_doc", "set": s, **agg(items, k),
                   "false_no_match": round(sum(1 for r in res if r["status"] == "no_match") / max(len(res), 1), 3),
                   "cited_recall@5": round(statistics.mean(1.0 if set(r["citations"]) & set(r["relevant"]) else 0.0 for r in res), 3) if res else None}
            f.write(json.dumps(rep, ensure_ascii=False) + "\n")
            print(f"graph {s:<6} n={rep['n']} R@1 {rep['recall@1']} R@5(kept) {rep[f'recall@{k}']} cited {rep['cited_recall@5']} false_no_match {rep['false_no_match']}")


def table(k: int) -> None:
    rows = load_jsonl(OUT)
    latest: dict[tuple, dict] = {}
    for r in rows:
        latest[(r["method"], r["switch"], r["set"])] = r
    keys = sorted({(m, sw) for m, sw, _ in latest})
    print("| method · 스위치 | " + " | ".join(f"{s} R@1 / R@5" for s in SETS) + " |")
    print("|---|" + "---|" * len(SETS))
    for m, sw in keys:
        cells = []
        for s in SETS:
            r = latest.get((m, sw, s))
            cells.append(f"{r['recall@1']} / {r[f'recall@{k}']}" + (f" (no_match {r['false_no_match']})" if r and "false_no_match" in r else "") if r else "—")
        print(f"| {m} · {sw} | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="vector,hybrid+filter")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--table", action="store_true")
    args = ap.parse_args()
    if args.graph:
        graph_summary(args.k)
    elif not args.table:
        for m in args.methods.split(","):
            run(m, args.k)
    table(args.k)


if __name__ == "__main__":
    main()
