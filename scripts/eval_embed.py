"""Step 12 — 임베딩 모델 비교(검색 단독, graph·cross-encoder·cosine 안전선 없음).

모델은 EMBED_MODEL 환경변수로 고른다(rag.config가 import 시점에 읽으므로 모델마다 프로세스를 따로 띄운다).
  EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B python -m scripts.eval_embed index        # 색인 + 시간·메모리 기록
  EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B python -m scripts.eval_embed eval         # rag.eval 두 method + 코드 질의 3종
  python -m scripts.eval_embed index --dry                                         # 기준 bge-m3: 임베딩 시간만, 컬렉션은 안 건드림
  python -m scripts.eval_embed eval
  python -m scripts.eval_embed report                                              # 표 + 실패 집합 비교(markdown)

바뀌는 것은 벡터 컬렉션(manufacturing_<모델slug>_v1)뿐 — BM25 색인·Kiwi 사전·메타데이터 필터·RRF는 그대로.
결과: domains/<domain>/eval/embed_compare/<slug>.json (행별 top-5 포함, 실패 집합 비교의 원자료).
"""
import argparse
import json
import resource
import statistics
import sys
import time

from rag import config

OUT_DIR = config.EVAL_DIR / "embed_compare"
MODELS = ["BAAI/bge-m3", "Qwen/Qwen3-Embedding-0.6B", "nlpai-lab/KURE-v1"]
METHODS = ("vector", "hybrid+filter")
CODE_QUERIES = [("E-1101", "E-1101"), ("PECVD-03 E-1101", "E-1101"), ("E-5201", "E-5201")]   # (질의, 맞아야 하는 알람 코드)
GATE_SOURCES = ("llm", "claude_chat_draft")        # 3주차 170행 기준(positive 139)


def _path(model: str = config.EMBED_MODEL):
    return OUT_DIR / f"{config._slug(model)}.json"


def _load(model: str) -> dict:
    p = _path(model)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save(update: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cur = _load(config.EMBED_MODEL)
    cur.update({"embed_model": config.EMBED_MODEL, "collection": config.COLLECTION, **update})
    _path().write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")


def _memory() -> dict:
    import torch
    out = {"rss_peak_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024))}
    if torch.backends.mps.is_available():
        out["mps_driver_mb"] = round(torch.mps.driver_allocated_memory() / 1024 / 1024)   # Metal 할당(RSS에 안 잡히는 몫)
    return out


def stage_index(dry: bool) -> None:
    from rag import embed, index
    from rag.documents import doc_text, load_tickets
    if not dry and config.EMBED_MODEL == config.DEFAULT_EMBED_MODEL:
        sys.exit("기준 컬렉션(bge-m3)은 다시 만들지 않는다 — --dry로 임베딩 시간만 잰다")
    tickets = load_tickets()
    t0 = time.perf_counter()
    m = embed.model()
    load_sec = time.perf_counter() - t0
    texts = [doc_text(t) for t in tickets]
    n_tok = [len(x) for x in m.tokenizer(texts, truncation=False)["input_ids"]]
    t0 = time.perf_counter()
    if dry:
        embed.embed(texts, kind="passage")
        n = len(texts)
    else:
        n = index.build(tickets)
    index_sec = time.perf_counter() - t0
    info = {"n_docs": n, "dry": dry, "load_sec": round(load_sec, 1), "index_sec": round(index_sec, 1),
            "device": str(m.device), "dim": embed.dim(), "max_seq_length": m.max_seq_length, "batch": config.EMBED_BATCH,
            "doc_tokens_mean": round(statistics.mean(n_tok), 1), "doc_tokens_max": max(n_tok), **_memory(),
            "at": time.strftime("%Y-%m-%dT%H:%M")}
    _save({"index_dry" if dry and config.EMBED_MODEL != config.DEFAULT_EMBED_MODEL else "index": info})
    print(json.dumps({"embed_model": config.EMBED_MODEL, "collection": config.COLLECTION, **info}, ensure_ascii=False, indent=2))


def stage_eval() -> None:
    from rag import embed
    from rag import eval as rag_eval
    from rag.index import client
    from rag.retrieve import search
    n_points = client().count(config.COLLECTION).count
    out: dict = {"query_prompt": embed.query_prompt(), "device": str(embed.model().device), "n_points": n_points, "methods": {}}
    for method in METHODS:
        rows: list = []
        report = rag_eval.run(method, per_row=rows)
        codes = {}
        for q, code in CODE_QUERIES:
            hits = search(q, k=5, method=method)
            codes[q] = {"hit": sum(h.payload.get("alarm_code") == code for h in hits),
                        "equipment": [h.payload.get("equipment_id") for h in hits]}
        out["methods"][method] = {"report": report, "code_queries": codes, "rows": rows}
        print(method, json.dumps({k: report.get(k) for k in ("human", "overall", "negative")}, ensure_ascii=False), codes, flush=True)
    _save({"eval": out, **({} if "index" in _load(config.EMBED_MODEL) else {"index": {}})})


def _agg(rows: list[dict]) -> str:
    if not rows:
        return "-"
    return " / ".join(f"{statistics.mean(r[k] for r in rows):.3f}" for k in ("r1", "r5", "rr"))


def _subset(d: dict, method: str, pred) -> list[dict]:
    return [r for r in d["eval"]["methods"][method]["rows"] if pred(r)]


def is_human(r): return r["difficulty"] == "human"
def is_170(r): return r["difficulty"] != "negative" and r["source"] in GATE_SOURCES
def is_neg(r): return r["difficulty"] == "negative"


def stage_report() -> None:
    data = {m: _load(m) for m in MODELS}
    data = {m: d for m, d in data.items() if d.get("eval")}
    base = MODELS[0]
    print("| 모델 | 124 vector R@1 / R@5 / MRR | 124 hybrid+filter R@1 / R@5 / MRR | negative 39 top1 cosine mean / max (vector) | 〃 (hybrid+filter) | 코드 3종 vector | 코드 3종 hybrid+filter | 170행 positive 139 R@5 (vector · h+f) | 색인 초 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for m, d in data.items():
        cells = [m]
        for method in METHODS:
            cells.append(_agg(_subset(d, method, is_human)))
        for method in METHODS:
            cs = [r["top1_cosine"] for r in _subset(d, method, is_neg) if r["top1_cosine"] is not None]
            cells.append(f"{statistics.mean(cs):.3f} / {max(cs):.3f} (n={len(cs)})")
        for method in METHODS:
            cq = d["eval"]["methods"][method]["code_queries"]
            cells.append(" · ".join(str(cq[q]["hit"]) for q, _ in CODE_QUERIES))
        cells.append(" · ".join(_agg(_subset(d, method, is_170)).split(" / ")[1] for method in METHODS))
        ix = d.get("index") or {}
        cells.append(f"{ix.get('index_sec', '-')} ({ix.get('device', '-')}{', 임베딩만' if ix.get('dry') else ''}) · load {ix.get('load_sec', '-')} · RSS {ix.get('rss_peak_mb', '-')}MB + MPS {ix.get('mps_driver_mb', '-')}MB")
        print("| " + " | ".join(cells) + " |")

    print("\n170행(positive 139) R@1 / R@5 / MRR:")
    for m, d in data.items():
        print(f"- {m}: " + " · ".join(f"{method} {_agg(_subset(d, method, is_170))}" for method in METHODS))
    print("\nnegative와 겹침(vector): human 124 중 top1 cosine ≤ negative max인 행 수 / positive 244 top1 cosine min")
    for m, d in data.items():
        neg = max(r["top1_cosine"] for r in _subset(d, "vector", is_neg))
        pos = [r["top1_cosine"] for r in _subset(d, "vector", lambda r: not is_neg(r))]
        hum = [r["top1_cosine"] for r in _subset(d, "vector", is_human)]
        print(f"- {m}: neg max {neg:.3f} · human ≤ neg max {sum(c <= neg for c in hum)}/124 · positive min {min(pos):.3f} · positive < 0.50: {sum(c < 0.5 for c in pos)}/{len(pos)} · negative < 0.50: {sum(r['top1_cosine'] < 0.5 for r in _subset(d, 'vector', is_neg))}/39")

    if base not in data:
        return
    for m, d in data.items():
        if m == base:
            continue
        for method in METHODS:
            b = {r["query"]: r for r in _subset(data[base], method, is_human)}
            n = {r["query"]: r for r in _subset(d, method, is_human)}
            for key, label in (("r5", "R@5"), ("r1", "R@1")):
                won = [q for q in b if not b[q][key] and n[q][key]]
                lost = [q for q in b if b[q][key] and not n[q][key]]
                both = sum(1 for q in b if not b[q][key] and not n[q][key])
                print(f"\n### {m} vs {base} — {method}, {label} (124행): 새 모델만 맞춤 {len(won)} · bge-m3만 맞춤 {len(lost)} · 둘 다 틀림 {both}")
                if key == "r5":
                    for title, qs in (("bge-m3 틀림 → 새 모델 맞춤", won), ("bge-m3 맞춤 → 새 모델 틀림", lost)):
                        print(f"**{title}** ({len(qs)}건)")
                        for q in qs:
                            print(f"- [{b[q]['source']}] {q}\n  - bge-m3 top5 {b[q]['top']} · 새 모델 top5 {n[q]['top']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["index", "eval", "report"])
    ap.add_argument("--dry", action="store_true", help="index: 임베딩 시간만 재고 컬렉션은 쓰지 않는다(기준 bge-m3용)")
    a = ap.parse_args()
    {"index": lambda: stage_index(a.dry), "eval": stage_eval, "report": stage_report}[a.stage]()
