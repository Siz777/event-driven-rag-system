"""평가 게이트(MLOps-lite). 고정 평가셋의 핵심 지표가 기준치 밑이면 exit 1 — 검색·판정·리랭커 manifest가 조용히 나빠지는 것을 막는다.

python -m scripts.eval_gate                       # recorded: experiments.jsonl 최신 행 + reranker_manifest.json 검증 (데이터·모델·LLM 불필요 → CI)
python -m scripts.eval_gate --mode live           # 검색을 실제로 다시 돌린다(bge-m3·Qdrant·BM25 색인 필요, LLM 0회). graph는 기록값
python -m scripts.eval_gate --mode live --graph live   # + graph를 val 65에서 재실행(LLM 호출 ≈ 65×6, ≈ $0.5)

기준치 = 측정값 − 0.02(2026-11-09 기준). 지표를 올리면 여기 숫자도 같이 올린다(기준치를 내리는 커밋은 이유를 일지에).
| 지표 | 평가셋 | 측정 | 기준 |
| vector R@5 | 170행 기준 positive 139 (source llm 120 + claude_chat_draft 19) | 0.885 | ≥ 0.865 |
| hybrid+filter R@5 | 같음 | 0.885 | ≥ 0.865 |
| graph(api) negative no_match | val 65 (negative 8) | 0.875 | ≥ 0.855 |
| graph(api) positive false no_match | val 65 (positive 57) | 0.0 | ≤ 0.02 |
| reranker manifest val neg_no_match / false_no_match | val 65 | 1.0 / 0.070 | ≥ 0.98 / ≤ 0.09, threshold∈(0,1), band lo ≤ hi, max_length>0 |
graph를 LLM으로 매번 재는 건 비용 때문에 기본에서 뺐다(기록값 검증). human 124행은 R@5 0.75로 아직 움직이는 중이라 게이트 밖(보고만).
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from rag import config
from rag.documents import load_jsonl

GATE_SOURCES = ("llm", "claude_chat_draft")        # 3주차 170행 기준
THRESHOLDS = {
    "vector.recall@5": ("ge", 0.865), "hybrid+filter.recall@5": ("ge", 0.865),
    "graph.negative_no_match": ("ge", 0.855), "graph.positive_false_no_match": ("le", 0.02),
    "reranker.val.neg_no_match": ("ge", 0.98), "reranker.val.false_no_match": ("le", 0.09),
}
EXPERIMENTS = config.EVAL_DIR / "experiments.jsonl"
MANIFEST = config.EVAL_DIR / "reranker_manifest.json"
GATE_LOG = config.EVAL_DIR / "gate_results.jsonl"


def _ok(op: str, value: float, thr: float) -> bool:
    return value >= thr if op == "ge" else value <= thr


def weighted_r5(by_source: dict) -> float | None:
    xs = [(by_source[s]["n"], by_source[s]["recall@5"]) for s in GATE_SOURCES if s in by_source]
    return round(sum(n * r for n, r in xs) / sum(n for n, _ in xs), 3) if xs else None


def recorded_retrieval(rows: list[dict]) -> dict[str, float | None]:
    out = {}
    for m in ("vector", "hybrid+filter"):
        cands = [r for r in rows if r.get("method") == m and r.get("eval_file", "eval.jsonl") == "eval.jsonl" and "by_source" in r]
        out[f"{m}.recall@5"] = weighted_r5(cands[-1]["by_source"]) if cands else None
    return out


def recorded_graph(rows: list[dict]) -> dict[str, float | None]:
    cands = [r for r in rows if r.get("method") == "graph(hybrid+filter+grade:per_doc)" and r.get("split") == "val" and "graph" in r]
    if not cands:
        return {"graph.negative_no_match": None, "graph.positive_false_no_match": None}
    g = cands[-1]["graph"]
    return {"graph.negative_no_match": g["negative"]["rate"], "graph.positive_false_no_match": g["positive"]["rate"]}


def live_retrieval(k: int = 5) -> dict[str, float]:
    from rag.retrieve import search
    rows = [r for r in load_jsonl(config.EVAL_DIR / "eval.jsonl") if r["difficulty"] != "negative" and r.get("source", "llm") in GATE_SOURCES]
    out = {}
    for m in ("vector", "hybrid+filter"):
        hits_ok = [1.0 if set(r["relevant_ticket_ids"]) & {h.ticket_id for h in search(r["query"], k=k, method=m)} else 0.0 for r in rows]
        out[f"{m}.recall@5"] = round(statistics.mean(hits_ok), 3)
    out["_n_positive"] = len(rows)
    return out


def live_graph() -> dict[str, float | None]:
    out = config.EVAL_DIR / f"gate_graph_val_{time.strftime('%Y%m%d_%H%M')}.jsonl"
    subprocess.run([sys.executable, "-m", "scripts.eval_graph", "--mode", "per_doc", "--split", "val", "--no-clarify", "--out", str(out)],
                   check=True)
    return recorded_graph(load_jsonl(EXPERIMENTS))        # eval_graph가 experiments.jsonl에 행을 남기므로 그 최신 행


def check_manifest(path: Path = MANIFEST) -> tuple[dict[str, float | None], list[str]]:
    problems: list[str] = []
    vals: dict[str, float | None] = {"reranker.val.neg_no_match": None, "reranker.val.false_no_match": None}
    if not path.exists():
        return vals, [f"manifest missing: {path}"]
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return vals, [f"manifest not JSON: {e}"]
    for key in ("name", "source_model", "threshold", "band", "val", "max_length"):
        if key not in m:
            problems.append(f"manifest lacks '{key}'")
    thr = m.get("threshold")
    if not (isinstance(thr, (int, float)) and 0 < thr < 1):
        problems.append(f"threshold out of (0,1): {thr}")
    band = m.get("band") or []
    if not (isinstance(band, list) and len(band) == 2 and band[0] <= band[1]):
        problems.append(f"band malformed: {band}")
    elif thr is not None and abs(band[0] - thr) > 1e-9:
        problems.append(f"band lo {band[0]} != threshold {thr} (two_stage lo = threshold, 11-09 결정)")
    if not (isinstance(m.get("max_length"), int) and m["max_length"] > 0):
        problems.append(f"max_length invalid: {m.get('max_length')}")
    v = m.get("val") or {}
    vals["reranker.val.neg_no_match"] = v.get("neg_no_match")
    vals["reranker.val.false_no_match"] = v.get("false_no_match")
    best = config.DOMAIN_DIR / "artifacts" / "reranker" / "best" / "manifest.json"
    if path == MANIFEST and best.exists() and json.loads(best.read_text(encoding="utf-8")).get("name") != m.get("name"):
        problems.append("artifacts/reranker/best/manifest.json name differs from tracked eval/reranker_manifest.json — run pick")
    return vals, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["recorded", "live"], default="recorded")
    ap.add_argument("--graph", choices=["recorded", "live"], default="recorded")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()
    rows = load_jsonl(EXPERIMENTS) if EXPERIMENTS.exists() else []
    values: dict[str, float | None] = {}
    values.update(live_retrieval() if args.mode == "live" else recorded_retrieval(rows))
    values.update(live_graph() if args.graph == "live" else recorded_graph(rows))
    mvals, problems = check_manifest()
    values.update(mvals)
    failed = list(problems)
    print(f"{'metric':36s} {'value':>8s} {'threshold':>10s}  result")
    for key, (op, thr) in THRESHOLDS.items():
        v = values.get(key)
        ok = v is not None and _ok(op, v, thr)
        if not ok:
            failed.append(f"{key}={v} not {op} {thr}")
        print(f"{key:36s} {('-' if v is None else f'{v:.3f}'):>8s} {('≥' if op == 'ge' else '≤') + f' {thr:.3f}':>10s}  {'PASS' if ok else 'FAIL'}")
    for p in problems:
        print("manifest:", p)
    if not args.no_log:
        with open(GATE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M"), "mode": args.mode, "graph": args.graph,
                                "values": {k: v for k, v in values.items() if not k.startswith("_")}, "failed": failed}, ensure_ascii=False) + "\n")
    print("GATE", "FAIL" if failed else "PASS", f"({len(failed)} problem(s))" if failed else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
