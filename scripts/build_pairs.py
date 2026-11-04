"""cross-encoder 판정 실험용 (query, ticket_text, label) 쌍 생성 (11-04, 실험 준비 — 학습은 다음).

eval.jsonl 전 행 × hybrid+filter 상위 20건 → 쌍 1개씩.
- label = 1 if 티켓 cause_id == 질의 정답 cause (positive 질의), negative 질의는 전부 0.
- confusable = 1 if 티켓 cause ∈ catalog confusable_with(정답 cause) (라벨은 0 — 어려운 음성. 별도 열이라 학습 때 가중치·제외 선택 가능).
- 분리는 **질의 ID 기준**(같은 질의의 쌍이 train/val에 갈라지지 않게). source=human 10건은 val 전용(학습에 사람 질의 안 씀 —
  사람 문체 일반화를 val에서 본다). 나머지는 difficulty별 층화로 VAL_RATIO(0.2) 분리, seed 고정.
- 출력: domains/<domain>/eval/pairs_train.jsonl, pairs_val.jsonl (gitignore — 티켓 본문이 들어가 크다), pairs_stats.json.
사용: python -m scripts.build_pairs [--k 20] [--val-ratio 0.2] [--seed 7]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict

import yaml

from rag import config
from rag.documents import doc_text, load_jsonl, load_tickets
from rag.retrieve import search

VAL_RATIO = 0.2


def qid(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]


def catalog_confusable() -> dict[str, set[str]]:
    cat = yaml.safe_load(open(config.DOMAIN_DIR / "catalog.yaml", encoding="utf-8"))
    items = cat if isinstance(cat, list) else cat.get("causes", cat)
    conf = {c["cause_id"]: set(c.get("confusable_with") or []) for c in items}
    for a, bs in list(conf.items()):
        for b in bs:
            conf.setdefault(b, set()).add(a)
    return conf


def split_queries(rows: list[dict], val_ratio: float, seed: int) -> dict[str, str]:
    """qid → train|val. human source는 val 고정, 나머지는 difficulty별 층화."""
    rng = random.Random(seed)
    out: dict[str, str] = {}
    by_diff: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("source") == "human":
            out[qid(r["query"])] = "val"
        else:
            by_diff[r["difficulty"]].append(r)
    for d, rs in sorted(by_diff.items()):
        rs = sorted(rs, key=lambda r: qid(r["query"]))
        rng.shuffle(rs)
        n_val = max(1, round(len(rs) * val_ratio))
        for i, r in enumerate(rs):
            out[qid(r["query"])] = "val" if i < n_val else "train"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--val-ratio", type=float, default=VAL_RATIO)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--method", default="hybrid+filter")
    args = ap.parse_args()

    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    tickets = {t["ticket_id"]: t for t in load_tickets()}
    conf = catalog_confusable()
    split = split_queries(rows, args.val_ratio, args.seed)
    files = {s: open(config.EVAL_DIR / f"pairs_{s}.jsonl", "w", encoding="utf-8") for s in ("train", "val")}
    stats: dict = {"k": args.k, "method": args.method, "queries": Counter(), "pairs": Counter(), "positives": Counter(),
                   "confusables": Counter(), "queries_without_positive_in_topk": Counter(), "by_difficulty": {}}
    bydiff: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        q, gold = r["query"], r.get("cause_id") or ""
        s = split[qid(q)]
        hits = search(q, k=args.k, method=args.method)
        stats["queries"][s] += 1
        has_pos = False
        for rank, h in enumerate(hits, 1):
            t = tickets.get(h.ticket_id) or h.payload
            tc = t.get("cause_id", "")
            label = int(bool(gold) and tc == gold)
            confusable = int(bool(gold) and tc in conf.get(gold, set()))
            has_pos |= bool(label)
            rec = {"qid": qid(q), "query": q, "difficulty": r["difficulty"], "source": r.get("source", "llm"),
                   "gold_cause": gold, "ticket_id": h.ticket_id, "ticket_cause": tc, "ticket_text": doc_text(t),
                   "root_cause": t.get("root_cause", ""), "label": label, "confusable": confusable, "rank": rank,
                   "score": round(h.score, 5), "cosine": round(h.cosine, 4) if h.cosine is not None else None}
            files[s].write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["pairs"][s] += 1; stats["positives"][s] += label; stats["confusables"][s] += confusable
            bydiff[r["difficulty"]]["pairs"] += 1; bydiff[r["difficulty"]]["positives"] += label
        if gold and not has_pos:
            stats["queries_without_positive_in_topk"][s] += 1
    for f in files.values():
        f.close()
    stats["positive_ratio"] = {s: round(stats["positives"][s] / max(stats["pairs"][s], 1), 3) for s in ("train", "val")}
    stats["by_difficulty"] = {d: {**c, "positive_ratio": round(c["positives"] / max(c["pairs"], 1), 3)} for d, c in sorted(bydiff.items())}
    stats["val_sources"] = dict(Counter(r.get("source", "llm") for r in rows if split[qid(r["query"])] == "val"))
    stats = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in stats.items()}
    json.dump(stats, open(config.EVAL_DIR / "pairs_stats.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
