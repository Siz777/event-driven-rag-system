"""그래프(retrieve → grade → generate|no_answer) 전체 평가. 실제 LLM을 쓴다 — 170행 × (판정 ≤5 + 생성 ≤1) 호출.

python -m scripts.eval_graph [--workers 3] [--out domains/.../eval/graph_results.jsonl]
- 결과는 질의별 jsonl로 바로 기록(중단 후 재실행 시 끝난 질의는 건너뜀).
- 지표: negative 31건 no_match 비율(목표 ≥0.9), positive 139건 잘못 no_match 비율(목표 ≤0.05),
  grade가 거른 문서로 인한 R@5 변화(검색 top-5 vs 판정 통과 집합), 질의당 LLM 호출 수·노드별 지연.
- 요약은 --summary 로 다시 뽑을 수 있다(LLM 호출 없음).
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from events import InboundEvent
from rag import config
from rag.documents import load_jsonl
from rag.graph import default_search
from rag.pipeline import answer

OUT = config.EVAL_DIR / "graph_results.jsonl"
MODE = "per_doc"
_SEARCH_LOCK = threading.Lock()   # bge-m3(MPS)·Qdrant local은 스레드 동시 호출에 안전하지 않다 — 검색만 직렬화, LLM 호출은 병렬


def locked_search(query: str, k: int):
    with _SEARCH_LOCK:
        return default_search(query, k)


def run_one(row: dict) -> dict:
    inb = InboundEvent(event_id="eval", source="cli", channel="terminal", user="eval", text=row["query"])
    trace: dict = {}
    t0 = time.monotonic()
    try:
        out = answer(inb, trace=trace, search=locked_search, grade_mode=MODE)
        err = None
    except Exception as e:                      # 평가는 계속 — 실패는 따로 센다
        out, err = None, f"{type(e).__name__}: {e}"[:200]
    return {"query": row["query"], "difficulty": row["difficulty"], "neg_type": row.get("neg_type"),
            "source": row.get("source", "llm"), "relevant": row["relevant_ticket_ids"],
            "source_ticket_id": row.get("source_ticket_id", ""),
            "status": out.status if out else "error", "error": err,
            "citations": [c.ticket_id for c in out.citations] if out else [],
            "text": (out.text if out else "")[:300], **trace, "wall": time.monotonic() - t0}


def summarize(results: list[dict]) -> dict:
    neg = [r for r in results if r["difficulty"] == "negative"]
    pos = [r for r in results if r["difficulty"] != "negative"]
    errs = [r for r in results if r["status"] == "error"]
    s: dict = {"n": len(results), "errors": len(errs)}
    s["negative"] = {"n": len(neg), "no_match": sum(r["status"] == "no_match" for r in neg),
                     "rate": round(sum(r["status"] == "no_match" for r in neg) / max(len(neg), 1), 3),
                     "gate": dict(Counter(r.get("gate") for r in neg)),
                     "by_neg_type": {t: {"n": len(xs), "no_match": sum(r["status"] == "no_match" for r in xs)}
                                     for t, xs in defaultdict(list, {k: [r for r in neg if r.get("neg_type") == k]
                                                                      for k in {r.get("neg_type") for r in neg}}).items()}}
    fp = [r for r in pos if r["status"] == "no_match"]
    s["positive"] = {"n": len(pos), "false_no_match": len(fp), "rate": round(len(fp) / max(len(pos), 1), 3),
                     "gate": dict(Counter(r.get("gate") for r in pos)),
                     "false_no_match_by_difficulty": dict(Counter(r["difficulty"] for r in fp))}
    # R@5: 검색 top-5 vs 판정 통과(kept) vs 인용(citations)
    def rec(key):
        by = defaultdict(list)
        for r in pos:
            got = set(r.get(key) or []); by[r["difficulty"]].append(1.0 if got & set(r["relevant"]) else 0.0)
        tbl = {d: round(statistics.mean(v), 3) for d, v in sorted(by.items())}
        tbl["overall"] = round(statistics.mean(x for v in by.values() for x in v), 3)
        return tbl
    s["recall@5"] = {"retrieved": rec("hits"), "after_grade": rec("kept"), "cited": rec("citations")}
    # 판정 통계: positive에서 정답 티켓이 kept에서 빠진 건수
    dropped_relevant = sum(1 for r in pos if set(r.get("hits") or []) & set(r["relevant"]) and not set(r.get("kept") or []) & set(r["relevant"]))
    s["grade"] = {"positive_relevant_dropped": dropped_relevant,
                  "yes_rate_positive": round(statistics.mean(sum(r.get("graded") or []) / max(len(r.get("graded") or []), 1) for r in pos), 3),
                  "yes_rate_negative": round(statistics.mean(sum(r.get("graded") or []) / max(len(r.get("graded") or []), 1) for r in neg), 3)}
    ok = [r for r in results if r["status"] != "error"]
    t = lambda key: round(statistics.mean(r["timings"].get(key, 0.0) for r in ok), 2)
    s["cost_latency"] = {"llm_calls_per_query": round(statistics.mean(r.get("llm_calls", 0) for r in ok), 2),
                         "llm_calls_total": sum(r.get("llm_calls", 0) for r in ok),
                         "mean_sec": {"retrieve": t("retrieve"), "grade": t("grade"), "generate": t("generate"), "total": t("total")},
                         "before_graph_est_sec": round(t("retrieve") + t("generate"), 2),
                         "p50_total": round(statistics.median(r["timings"]["total"] for r in ok), 2),
                         "p95_total": round(sorted(r["timings"]["total"] for r in ok)[int(0.95 * (len(ok) - 1))], 2)}
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--summary", action="store_true", help="기존 결과만 집계")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--mode", choices=["per_doc", "batch"], default="per_doc", help="GRADE_MODE")
    args = ap.parse_args()
    global MODE
    MODE = args.mode
    out = Path(args.out if args.mode == "per_doc" or args.out != str(OUT) else OUT.with_name("graph_results_batch.jsonl"))
    done = {r["query"]: r for r in load_jsonl(out)} if out.exists() else {}
    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    todo = [r for r in rows if r["query"] not in done or done[r["query"]]["status"] == "error"]
    if args.limit:
        todo = todo[:args.limit]
    if not args.summary and todo:
        out.parent.mkdir(parents=True, exist_ok=True)
        locked_search("워밍업 PECVD-01 E-1101", 1)       # 모델·색인을 메인 스레드에서 먼저 로드(스레드 동시 로드는 크래시)
        with ThreadPoolExecutor(max_workers=args.workers) as ex, open(out, "a", encoding="utf-8") as f:
            futs = {ex.submit(run_one, r): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                res = fut.result()
                done[res["query"]] = res
                f.write(json.dumps(res, ensure_ascii=False) + "\n"); f.flush()
                print(f"[{i}/{len(todo)}] {res['difficulty']:<16} {res['status']:<9} gate={res.get('gate')} calls={res.get('llm_calls')} "
                      f"{res['timings']['total'] if 'timings' in res else 0:.1f}s {res['query'][:40]}", flush=True)
    results = [done[r["query"]] for r in rows if r["query"] in done]
    s = summarize(results)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    with open(config.EVAL_DIR / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": f"graph(hybrid+filter+grade:{MODE})", "k": config.TOP_K, "doc_text_mode": config.DOC_TEXT_MODE,
                            "n": len(results), "at": time.strftime("%Y-%m-%dT%H:%M"), "graph": s,
                            "embed_model": config.EMBED_MODEL, "collection": config.COLLECTION,
                            "llm_model": config.LLM_MODEL, "grade_model": config.LLM_MODEL_SMALL}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
