"""설비 되묻기(Step 9) 평가 — human 124 + negative 39, 실제 LLM(grade). 질의별 jsonl로 기록, 재실행 시 이어서.

python -m scripts.eval_clarify [--workers 3] [--out domains/.../eval/clarify_results.jsonl] [--summary]

질의마다
 1. graph 1회(되묻기 on): status가 clarify면 "발동". negative에서 발동하면 "이력 없음"을 가린 것(집계).
 2. 발동한 positive는 오라클 답(정답 티켓의 equipment_type)을 합쳐(rewrite_followup) graph 1회 더 → 최종 hits/kept/citations.
 3. 지표: 발동 수(유형·출처별), human 124 R@5(검색 top-5 기준) before → after(발동 건만 오라클 적용), 상한 0.871과 비교.
    보조: 발동 안 한 실패는 그룹 R@5(정답 cause ∪ catalog confusable_with 안의 티켓이 top-5에 있으면 적중).
    비용: 발동 건의 LLM 호출 수(2회째 포함).
generate는 생략하지 않는다(실제 경로) — 비용을 줄이려면 --no-generate로 chat을 fake로 바꾼다(citations 집계는 빠짐).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from events import InboundEvent
from rag import config
from rag.clarify import rewrite_followup
from rag.documents import load_jsonl
from rag.graph import default_search
from rag.pipeline import answer

OUT = config.EVAL_DIR / "clarify_results.jsonl"
UPPER_BOUND_R5 = 0.871   # 11-03 오라클 필터 상한(human 124, hybrid+filter)
_SEARCH_LOCK = threading.Lock()
_CHAT = None   # --no-generate면 fake


def locked_search(query: str, k: int):
    with _SEARCH_LOCK:
        return default_search(query, k)


def catalog_maps() -> tuple[dict, dict, dict]:
    cat = yaml.safe_load(open(config.DOMAIN_DIR / "catalog.yaml", encoding="utf-8"))
    items = cat if isinstance(cat, list) else cat.get("causes", cat)
    etype = {c["cause_id"]: c["equipment_type"] for c in items}
    conf = {c["cause_id"]: set(c.get("confusable_with") or []) for c in items}
    for a, bs in list(conf.items()):             # 대칭 닫기
        for b in bs:
            conf.setdefault(b, set()).add(a)
    return etype, conf, {t["ticket_id"]: t["cause_id"] for t in load_jsonl(config.DATA_DIR / "tickets.jsonl")}


def sections() -> dict[str, str]:
    """docs/human_queries.txt의 '# --- 유형' 구간 → 질의별 유형."""
    out, cur = {}, "?"
    p = Path("docs/human_queries.txt")
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("# ---"):
            cur = line[5:].strip(); continue
        m = re.match(r"^(.+?)\s*[\t|,]\s*(C\d{2}|NEG)\s*(#human)?\s*$", line)
        if m:
            out[m[1].strip()] = cur
    return out


def _run(text: str, allow_clarify: bool = True) -> tuple[dict, dict]:
    inb = InboundEvent(event_id="eval", source="cli", channel="terminal", user="eval", text=text)
    trace: dict = {}
    kw = {"chat": _CHAT} if _CHAT else {}
    out = answer(inb, trace=trace, search=locked_search, allow_clarify=allow_clarify, **kw)
    return {"status": out.status, "citations": [c.ticket_id for c in out.citations], "text": out.text[:200]}, trace


def run_one(row: dict, etype: dict) -> dict:
    t0 = time.monotonic()
    rec = {"query": row["query"], "difficulty": row["difficulty"], "source": row.get("source", "llm"),
           "cause_id": row.get("cause_id", ""), "relevant": row["relevant_ticket_ids"]}
    try:
        first, tr = _run(row["query"])
        rec.update({"first": {**first, "hits": tr.get("hits"), "kept": tr.get("kept"), "candidates": tr.get("candidates"),
                              "gate": tr.get("gate"), "llm_calls": tr.get("llm_calls", 0)}})
        rec["asked"] = first["status"] == "clarify"
        if rec["asked"] and row["difficulty"] != "negative":
            gold = etype[row["cause_id"]]
            rec["oracle_type"] = gold
            rec["oracle_in_candidates"] = gold in (tr.get("candidates") or [])
            second, tr2 = _run(rewrite_followup(row["query"], gold))
            rec["second"] = {**second, "hits": tr2.get("hits"), "kept": tr2.get("kept"), "gate": tr2.get("gate"),
                             "llm_calls": tr2.get("llm_calls", 0)}
        rec["error"] = None
    except Exception as e:                       # 평가는 계속
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
        rec.setdefault("asked", False)
    rec["wall"] = round(time.monotonic() - t0, 1)
    return rec


def _mean(xs) -> float | None:
    xs = list(xs)
    return round(statistics.mean(xs), 3) if xs else None


def summarize(results: list[dict], conf: dict, tcause: dict, sec: dict) -> dict:
    ok = [r for r in results if not r.get("error")]
    pos = [r for r in ok if r["difficulty"] != "negative"]
    neg = [r for r in ok if r["difficulty"] == "negative"]
    s: dict = {"n": len(results), "errors": len(results) - len(ok), "positive": len(pos), "negative": len(neg)}

    def r5(ids, rel):
        return 1.0 if set(ids or []) & set(rel) else 0.0

    def r1(ids, rel):
        return 1.0 if ids and ids[0] in set(rel) else 0.0

    def final(r, key="hits"):
        return (r.get("second") or r["first"]).get(key)

    asked = [r for r in pos if r["asked"]]
    s["asked"] = {"positive": len(asked), "positive_rate": round(len(asked) / max(len(pos), 1), 3),
                  "negative": sum(r["asked"] for r in neg), "negative_rate": round(sum(r["asked"] for r in neg) / max(len(neg), 1), 3),
                  "by_source": dict(Counter(r["source"] for r in asked)),
                  "by_section": dict(Counter(sec.get(r["query"], "?") for r in asked)),
                  "oracle_in_candidates": sum(bool(r.get("oracle_in_candidates")) for r in asked),
                  "failed_before_and_asked": sum(1 for r in asked if r5(r["first"]["hits"], r["relevant"]) == 0)}
    before = {"recall@1": _mean(r1(r["first"]["hits"], r["relevant"]) for r in pos),
              "recall@5": _mean(r5(r["first"]["hits"], r["relevant"]) for r in pos)}
    after = {"recall@1": _mean(r1(final(r), r["relevant"]) for r in pos),
             "recall@5": _mean(r5(final(r), r["relevant"]) for r in pos),
             "recall@5_kept": _mean(r5(final(r, "kept"), r["relevant"]) for r in pos),
             "recall@5_cited": _mean(r5(final(r, "citations"), r["relevant"]) for r in pos)}
    s["human"] = {"n": len(pos), "before": before, "after_oracle_answer": after, "upper_bound_recall@5": UPPER_BOUND_R5,
                  "asked_subset": {"n": len(asked),
                                   "before_recall@5": _mean(r5(r["first"]["hits"], r["relevant"]) for r in asked) if asked else None,
                                   "after_recall@5": _mean(r5(final(r), r["relevant"]) for r in asked) if asked else None}}
    # 유형별 after R@5
    by_sec = defaultdict(list)
    for r in pos:
        by_sec[sec.get(r["query"], "?")].append((r5(r["first"]["hits"], r["relevant"]), r5(final(r), r["relevant"])))
    s["by_section"] = {k: {"n": len(v), "before": _mean(a for a, _ in v), "after": _mean(b for _, b in v)}
                       for k, v in sorted(by_sec.items())}
    # 보조: 발동 안 한 실패 → 그룹 R@5
    not_asked_fail = [r for r in pos if not r["asked"] and r5(r["first"]["hits"], r["relevant"]) == 0]
    def group_hit(r):
        grp = {r["cause_id"]} | conf.get(r["cause_id"], set())
        return 1.0 if any(tcause.get(t) in grp for t in (r["first"]["hits"] or [])) else 0.0
    s["not_asked_failures"] = {"n": len(not_asked_fail), "group_recall@5": _mean(group_hit(r) for r in not_asked_fail) if not_asked_fail else None,
                               "by_cause": dict(Counter(r["cause_id"] for r in not_asked_fail)),
                               "top1_cause": dict(Counter(tcause.get((r["first"]["hits"] or [""])[0], "?") for r in not_asked_fail))}
    still = [r for r in asked if r5(final(r), r["relevant"]) == 0]
    s["asked_still_failing"] = {"n": len(still), "group_recall@5": _mean(group_hit(r) for r in still) if still else None,
                                "by_cause": dict(Counter(r["cause_id"] for r in still))}
    # 상태 분포·비용
    s["final_status"] = {"positive": dict(Counter((r.get("second") or r["first"])["status"] for r in pos)),
                         "negative": dict(Counter(r["first"]["status"] for r in neg))}
    s["cost"] = {"llm_calls_per_query_not_asked": _mean(r["first"]["llm_calls"] for r in pos if not r["asked"]) if len(pos) > len(asked) else None,
                 "llm_calls_per_query_asked_total": _mean(r["first"]["llm_calls"] + r["second"]["llm_calls"] for r in asked) if asked else None,
                 "wall_mean_sec": _mean(r["wall"] for r in ok)}
    return s


def main() -> None:
    global _CHAT
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-generate", action="store_true", help="생성 LLM을 fake로(비용 절감, citations 집계 무의미)")
    args = ap.parse_args()
    if args.no_generate:
        _CHAT = lambda u, s: "[" + "] [".join(re.findall(r"INC-\d{4}-\d{4}", u)[:3]) + "]"   # noqa: E731
    etype, conf, tcause = catalog_maps()
    sec = sections()
    out = Path(args.out)
    done = {r["query"]: r for r in load_jsonl(out)} if out.exists() else {}
    rows = [r for r in load_jsonl(config.EVAL_DIR / "eval.jsonl") if r["difficulty"] in ("human", "negative")]
    todo = [r for r in rows if r["query"] not in done or done[r["query"]].get("error")]
    if args.limit:
        todo = todo[:args.limit]
    if not args.summary and todo:
        locked_search("워밍업 PECVD-01 E-1101", 1)
        with ThreadPoolExecutor(max_workers=args.workers) as ex, open(out, "a", encoding="utf-8") as f:
            futs = {ex.submit(run_one, r, etype): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                res = fut.result()
                done[res["query"]] = res
                f.write(json.dumps(res, ensure_ascii=False) + "\n"); f.flush()
                st = (res.get("second") or res.get("first") or {}).get("status", "error")
                print(f"[{i}/{len(todo)}] {res['difficulty']:<9} asked={int(res['asked'])} final={st:<9} {res['wall']:5.1f}s {res['query'][:40]}", flush=True)
    results = [done[r["query"]] for r in rows if r["query"] in done]
    s = summarize(results, conf, tcause, sec)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    if args.summary or args.limit:
        return                                   # 부분 집계는 실험 로그에 남기지 않는다
    with open(config.EVAL_DIR / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": "graph+clarify(oracle answer)", "k": config.TOP_K, "n": len(results),
                            "at": time.strftime("%Y-%m-%dT%H:%M"), "clarify": s, "embed_model": config.EMBED_MODEL,
                            "collection": config.COLLECTION, "llm_model": config.LLM_MODEL, "grade_model": config.LLM_MODEL_SMALL},
                           ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
