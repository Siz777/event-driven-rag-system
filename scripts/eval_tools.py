"""집계 도구 전후 평가(Step 11 Day 2). eval_aggregate.jsonl(95행)을 GRAPH_TOOLS off/on으로 그래프 전체 재실행 →
숫자 정확도(정답 = cause_id 기준 전체 이력 집계)·호출 적절성(expect_tool과 일치)·도구 결과 충실도·비용.

python -m scripts.eval_tools --tools off|on [--workers 3 --limit N]
python -m scripts.eval_tools --summary          # 두 결과 파일을 표로

숫자 판정(답변 본문 텍스트에서):
- count_hit: 정답 건수가 "N건/N회/N번/N 건" 으로 등장
- downtime_hit: 정답 평균(반올림 정수 또는 소수 1자리) 또는 중앙값이 "분/min" 단위 숫자로 등장(±1분)
- last_seen_hit: 최근 발생일(YYYY-MM-DD, M월 D일, M/D) 또는 최근 티켓 ID가 등장
- any_hit: 셋 중 하나. faithful(도구를 불렀을 때): 같은 판정을 도구가 실제로 돌려준 숫자로 — 모델이 받은 숫자를 그대로 썼는가.
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

from events import InboundEvent
from rag import config
from rag.documents import load_jsonl
from rag.pipeline import answer, default_search
from rag.tools import aggregate_tickets

EVAL = config.EVAL_DIR / "eval_aggregate.jsonl"
_LOCK = threading.Lock()


def locked_search(q, k):
    with _LOCK:
        return default_search(q, k)


def _nums(text: str, unit: str) -> list[float]:
    return [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*" + unit, text)]


def count_hit(text: str, count: int) -> bool:
    return bool(re.search(rf"(?<!\d){count}\s*(?:건|회|번|차례)", text))


def downtime_hit(text: str, mean: float | None, median: float | None) -> bool:
    if mean is None:
        return False
    found = _nums(text, r"(?:분|min)")
    targets = {round(mean), round(mean, 1), mean} | ({median, round(median)} if median is not None else set())
    return any(abs(f - t) <= 1.0 for f in found for t in targets)


def last_seen_hit(text: str, last_seen: str | None, recent_id: str | None) -> bool:
    if not last_seen:
        return False
    y, m, d = last_seen.split("-")
    pats = [re.escape(last_seen), rf"{int(m)}\s*월\s*{int(d)}\s*일", rf"(?<!\d){int(m)}/{int(d)}(?!\d)", rf"{y}\.\s*{int(m)}\.\s*{int(d)}"]
    if recent_id:
        pats.append(re.escape(recent_id))
    return any(re.search(p, text) for p in pats)


def score(text: str, gold: dict, recent_id: str | None) -> dict:
    c = count_hit(text, gold["count"]); dt = downtime_hit(text, gold.get("downtime_mean"), gold.get("downtime_median"))
    ls = last_seen_hit(text, gold.get("last_seen"), recent_id)
    return {"count_hit": c, "downtime_hit": dt, "last_seen_hit": ls, "any_hit": c or dt or ls}


def run_one(row: dict, tools: bool) -> dict:
    inb = InboundEvent(event_id="eval", source="cli", channel="terminal", user="eval", text=row["query"])
    tr: dict = {}
    t0 = time.monotonic()
    try:
        out = answer(inb, trace=tr, search=locked_search, tools=tools)
        err = None
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {e}"[:200]
    text = out.text if out else ""
    gold_recent = aggregate_tickets(cause_id=row["cause_id"])["recent_ticket_ids"][0]
    rec = {"query": row["query"], "group": row["group"], "expect_tool": row["expect_tool"], "cause_id": row["cause_id"], "gold": row["gold"],
           "status": out.status if out else "error", "error": err, "text": text, "citations": [c.ticket_id for c in out.citations] if out else [],
           "relevant": row["relevant_ticket_ids"], "tool_calls": tr.get("tool_calls", []), "llm_calls": tr.get("llm_calls"),
           "usage": tr.get("usage"), "timings": tr.get("timings", {}), "wall": time.monotonic() - t0, "tools": tools,
           **score(text, row["gold"], gold_recent)}
    tc = rec["tool_calls"][0] if rec["tool_calls"] else None
    rec["tool_called"] = tc is not None
    rec["appropriate"] = rec["tool_called"] == row["expect_tool"]
    if tc and tc.get("count"):
        rec["faithful"] = score(text, {"count": tc["count"], "downtime_mean": tc.get("mean"), "downtime_median": tc.get("median"),
                                       "last_seen": tc.get("last_seen")}, tc.get("recent"))["any_hit"]
        rec["tool_filter_is_gold_cause"] = (tc.get("args") or {}).get("cause_id", "").upper() == row["cause_id"]
    return rec


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] != "error"]
    def rate(xs, key): return round(sum(bool(x.get(key)) for x in xs) / max(len(xs), 1), 3)
    out = {}
    for g in ["multi_hop", "human_action_time", "human_history", "exact_control", "ALL"]:
        xs = ok if g == "ALL" else [r for r in ok if r["group"] == g]
        if not xs:
            continue
        called = [r for r in xs if r["tool_called"]]
        out[g] = {"n": len(xs), "answered": rate(xs, "status") and round(sum(r["status"] == "answered" for r in xs) / len(xs), 3),
                  "tool_call_rate": rate(xs, "tool_called"), "appropriate": rate(xs, "appropriate"),
                  "count_hit": rate(xs, "count_hit"), "downtime_hit": rate(xs, "downtime_hit"), "last_seen_hit": rate(xs, "last_seen_hit"),
                  "any_hit": rate(xs, "any_hit"),
                  "faithful_when_called": rate(called, "faithful") if called else None,
                  "filter_is_gold_cause_when_called": rate(called, "tool_filter_is_gold_cause") if called else None,
                  "cited_relevant_r@5": round(statistics.mean(1.0 if set(r["citations"]) & set(r["relevant"]) else 0.0 for r in xs), 3),
                  "llm_calls": round(statistics.mean(r["llm_calls"] or 0 for r in xs), 2),
                  "tokens": round(statistics.mean((r["usage"] or {}).get("total_tokens", 0) for r in xs)),
                  "cost_usd": round(statistics.mean((r["usage"] or {}).get("cost_usd", 0) or 0 for r in xs), 5),
                  "sec": round(statistics.mean(r["timings"].get("total", r["wall"]) for r in xs), 1)}
    out["errors"] = len(rows) - len(ok)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", choices=["off", "on"], default="on")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.summary:
        for mode in ("off", "on"):
            p = config.EVAL_DIR / f"aggregate_results_{mode}.jsonl"
            if p.exists():
                print(f"== tools {mode}"); print(json.dumps(summarize(load_jsonl(p)), ensure_ascii=False, indent=1))
        return
    tools = args.tools == "on"
    out = config.EVAL_DIR / f"aggregate_results_{args.tools}.jsonl"
    done = {r["query"]: r for r in load_jsonl(out)} if out.exists() else {}
    rows = load_jsonl(EVAL)
    todo = [r for r in rows if r["query"] not in done or done[r["query"]]["status"] == "error"]
    if args.limit:
        todo = todo[:args.limit]
    if todo:
        locked_search("워밍업 PECVD-01 E-1101", 1)
        with ThreadPoolExecutor(max_workers=args.workers) as ex, open(out, "a", encoding="utf-8") as f:
            futs = {ex.submit(run_one, r, tools): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                res = fut.result(); done[res["query"]] = res
                f.write(json.dumps(res, ensure_ascii=False) + "\n"); f.flush()
                print(f"[{i}/{len(todo)}] {res['group']:17s} {res['status']:9s} tool={'Y' if res['tool_called'] else '-'} "
                      f"hit={'C' if res['count_hit'] else '-'}{'D' if res['downtime_hit'] else '-'}{'L' if res['last_seen_hit'] else '-'} "
                      f"{res['wall']:.0f}s {res['query'][:40]}", flush=True)
    results = [done[r["query"]] for r in rows if r["query"] in done]
    s = summarize(results)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    with open(config.EVAL_DIR / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": f"graph(tools:{args.tools})", "eval_file": "eval_aggregate.jsonl", "n": len(results),
                            "at": time.strftime("%Y-%m-%dT%H:%M"), "aggregate": s, "llm_model": config.LLM_MODEL,
                            "grade_model": config.LLM_MODEL_SMALL}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
