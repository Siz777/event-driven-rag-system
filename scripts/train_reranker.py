"""cross-encoder 판정 실험 (Step 10): 제로샷 점수 → 임계값(train) → val 질의 지표, 파인튜닝(변형 2) → 체크포인트별 같은 지표 → best 저장.

python -m scripts.train_reranker score  --model BAAI/bge-reranker-v2-m3 --name zeroshot
python -m scripts.train_reranker train  --variant all|no_confusable [--epochs 2 --batch 16 --lr 2e-5 --model … --device cpu --max-length 256]
  (11-04 MPS 학습은 bge-reranker-base 2 step 만에 MPSLibrary 어서션으로 abort — 11-05부터 --device cpu 기본)
python -m scripts.train_reranker pick   # 지금까지의 결과 중 best → artifacts/reranker/best + manifest.json

지표(질의 단위, 그래프와 같은 규칙: 상위 5건, yes = 확률 ≥ 임계값, yes < 2건이면 no_match)
- negative no_match 비율(↑), positive false no_match 비율(↓), 정답 유실(상위 5에 정답이 있는데 yes 집합엔 없음, ↓), kept R@5
- 쌍 AUC(val 전체 1,288 / 상위 5만)
임계값은 train 질의에서 (neg_no_match − false_no_match − dropped)를 최대화하는 값(격자 = train 점수 분위수 200개). val엔 손대지 않는다.
결과는 eval/reranker_results.jsonl에 1행씩, 점수는 artifacts/reranker/scores_<name>.jsonl(재사용).
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from rag import config
from rag.documents import load_jsonl

ART = config.DOMAIN_DIR / "artifacts" / "reranker"
RESULTS = config.EVAL_DIR / "reranker_results.jsonl"
MIN_YES = 2
TOP_K = 5


def pairs(split: str) -> list[dict]:
    return load_jsonl(config.EVAL_DIR / f"pairs_{split}.jsonl")


def pair_input(p: dict) -> tuple[str, str]:
    return p["query"], f"{p['ticket_text']}\n최종 원인: {p.get('root_cause', '')}"


DEVICE: str | None = None      # --device (cpu|mps|None=auto)
MAX_LENGTH = 512               # --max-length


def score_with(model, rows: list[dict], batch: int = 32) -> list[float]:
    t0 = time.time()
    out = model.predict([pair_input(p) for p in rows], batch_size=batch, show_progress_bar=True)
    print(f"scored {len(rows)} pairs in {time.time() - t0:.0f}s ({(time.time() - t0) / max(len(rows), 1) * 1000:.0f} ms/pair)", flush=True)
    return [float(x) for x in out]


def score_pairs(model_path: str, rows: list[dict], batch: int = 32) -> list[float]:
    from sentence_transformers import CrossEncoder
    m = CrossEncoder(model_path, max_length=MAX_LENGTH, device=DEVICE)
    out = score_with(m, rows, batch)
    del m
    return out


def top5(rows: list[dict]) -> list[dict]:
    return [p for p in rows if p["rank"] <= TOP_K]


def query_metrics(rows: list[dict], scores: list[float], thr: float) -> dict:
    by: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for p, s in zip(rows, scores):
        if p["rank"] <= TOP_K:
            by[p["qid"]].append((p, s))
    neg_nm, pos_fnm, dropped, kept_hit, n_neg, n_pos = 0, 0, 0, 0, 0, 0
    for qid, ps in by.items():
        ps.sort(key=lambda x: x[0]["rank"])
        yes = [p for p, s in ps if s >= thr]
        no_match = len(yes) < min(MIN_YES, len(ps))
        if ps[0][0]["difficulty"] == "negative":
            n_neg += 1; neg_nm += no_match
        else:
            n_pos += 1
            pos_fnm += no_match
            gold_in_top = any(p["label"] for p, _ in ps)
            gold_kept = any(p["label"] for p in yes)
            dropped += gold_in_top and not gold_kept
            kept_hit += (not no_match) and gold_kept
    return {"n_neg": n_neg, "n_pos": n_pos, "neg_no_match": round(neg_nm / max(n_neg, 1), 3),
            "false_no_match": round(pos_fnm / max(n_pos, 1), 3), "dropped_relevant": dropped,
            "dropped_rate": round(dropped / max(n_pos, 1), 3), "kept_recall@5": round(kept_hit / max(n_pos, 1), 3)}


def choose_threshold(rows: list[dict], scores: list[float]) -> tuple[float, dict]:
    grid = np.unique(np.quantile(np.array(scores), np.linspace(0.0, 1.0, 201)))
    best, best_thr, best_m = -9.0, 0.5, {}
    for thr in grid:
        m = query_metrics(rows, scores, float(thr))
        obj = m["neg_no_match"] - m["false_no_match"] - m["dropped_rate"]
        if obj > best or (obj == best and m["neg_no_match"] > best_m.get("neg_no_match", -1)):
            best, best_thr, best_m = obj, float(thr), m
    return best_thr, best_m


def auc(rows: list[dict], scores: list[float], top_only: bool = False) -> float | None:
    from sklearn.metrics import roc_auc_score
    ys = [p["label"] for p, s in zip(rows, scores) if (p["rank"] <= TOP_K or not top_only)]
    ss = [s for p, s in zip(rows, scores) if (p["rank"] <= TOP_K or not top_only)]
    return round(float(roc_auc_score(ys, ss)), 4) if len(set(ys)) == 2 else None


def evaluate(name: str, model_path: str, train_rows: list[dict], val_rows: list[dict], extra: dict | None = None,
             train_scores: list[float] | None = None, val_scores: list[float] | None = None) -> dict:
    """train_rows는 상위 5건 쌍만이어도 된다(임계값·band·질의 지표가 전부 rank ≤ 5만 본다). val은 전체(쌍 AUC용)."""
    ART.mkdir(parents=True, exist_ok=True)
    if train_scores is None:
        train_scores = score_pairs(model_path, train_rows)
    if val_scores is None:
        val_scores = score_pairs(model_path, val_rows)
    with open(ART / f"scores_{name}.jsonl", "w", encoding="utf-8") as f:
        for split, rows, sc in (("train", train_rows, train_scores), ("val", val_rows, val_scores)):
            for p, s in zip(rows, sc):
                f.write(json.dumps({"split": split, "qid": p["qid"], "ticket_id": p["ticket_id"], "rank": p["rank"],
                                    "label": p["label"], "confusable": p["confusable"], "score": round(s, 5)}) + "\n")
    thr, train_m = choose_threshold(train_rows, train_scores)
    val_m = query_metrics(val_rows, val_scores, thr)
    by_src: dict[str, dict] = {}
    for src in sorted({p["source"] for p in val_rows}):
        sub = [(p, s) for p, s in zip(val_rows, val_scores) if p["source"] == src]
        by_src[src] = query_metrics([p for p, _ in sub], [s for _, s in sub], thr)
    conf = [(p, s) for p, s in zip(val_rows, val_scores) if p["confusable"] and p["rank"] <= TOP_K]
    res = {"name": name, "model": model_path, "at": time.strftime("%Y-%m-%dT%H:%M"), "threshold": round(thr, 4),
           "max_length": MAX_LENGTH, "device": DEVICE or "auto",
           "train": train_m, "val": val_m, "val_by_source": by_src,
           "val_auc_all": auc(val_rows, val_scores), "val_auc_top5": auc(val_rows, val_scores, True),
           "val_confusable_top5_yes_rate": round(sum(s >= thr for _, s in conf) / max(len(conf), 1), 3),
           "val_score_mean_pos": round(statistics.mean(s for p, s in zip(val_rows, val_scores) if p["label"]), 4),
           "val_score_mean_neg": round(statistics.mean(s for p, s in zip(val_rows, val_scores) if not p["label"]), 4),
           **(extra or {})}
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def train(args, train_rows: list[dict], val_rows: list[dict]) -> None:
    import torch
    from datasets import Dataset
    from sentence_transformers import CrossEncoder
    from sentence_transformers.cross_encoder import CrossEncoderTrainer, CrossEncoderTrainingArguments
    from sentence_transformers.cross_encoder.losses import BinaryCrossEntropyLoss
    from transformers import TrainerCallback

    rows = [p for p in train_rows if not (args.variant == "no_confusable" and p["confusable"])]
    ds = Dataset.from_dict({"query": [pair_input(p)[0] for p in rows], "text": [pair_input(p)[1] for p in rows],
                            "label": [float(p["label"]) for p in rows]})
    out_dir = ART / f"ft_{args.variant}_{Path(args.model).name}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    model = CrossEncoder(args.model, max_length=MAX_LENGTH, num_labels=1, device=DEVICE)
    targs = CrossEncoderTrainingArguments(
        output_dir=str(out_dir), num_train_epochs=args.epochs, per_device_train_batch_size=args.batch,
        learning_rate=args.lr, warmup_ratio=0.1, save_strategy="epoch", save_total_limit=args.epochs,
        logging_steps=25, report_to="none", dataloader_num_workers=0, seed=args.seed,
        fp16=False, bf16=False, use_cpu=(DEVICE == "cpu"))
    tr5 = top5(train_rows)                     # 임계값·질의 지표는 상위 5건 쌍만 본다 → 에폭마다 1,090 + val 1,288 쌍만 채점
    extra = {"variant": args.variant, "epochs": args.epochs, "batch": args.batch, "lr": args.lr,
             "train_pairs": len(rows), "base_model": args.model}

    class EpochEval(TrainerCallback):
        """체크포인트 저장 직후(에폭 끝) 학습 중인 모델로 val을 채점 — 에폭별 val AUC·false no_match를 바로 로그에 남긴다."""
        def on_save(self, targs_, state, control, **kw):
            ep = int(round(state.epoch))
            ck = out_dir / f"checkpoint-{state.global_step}"
            print(f"\n[epoch {ep}] scoring train top5 {len(tr5)} + val {len(val_rows)} pairs …", flush=True)
            res = evaluate(f"ft_{args.variant}_e{ep}", str(ck), tr5, val_rows,
                           extra={**extra, "epoch": ep, "step": state.global_step},
                           train_scores=score_with(model, tr5), val_scores=score_with(model, val_rows))
            v = res["val"]
            print(f"[epoch {ep}] val AUC {res['val_auc_all']} (top5 {res['val_auc_top5']}) | thr {res['threshold']} | "
                  f"neg_no_match {v['neg_no_match']} | false_no_match {v['false_no_match']} | dropped {v['dropped_relevant']} | "
                  f"kept R@5 {v['kept_recall@5']} | loss {state.log_history[-1].get('loss') if state.log_history else None}",
                  flush=True)

    trainer = CrossEncoderTrainer(model=model, args=targs, train_dataset=ds, loss=BinaryCrossEntropyLoss(model),
                                  callbacks=[EpochEval()])
    print(f"training {args.variant}: {len(rows)} pairs (positives {sum(p['label'] for p in rows)}), {args.epochs} epochs, "
          f"batch {args.batch}, lr {args.lr}, model {args.model}, device {model.device}, max_length {MAX_LENGTH}, "
          f"torch threads {torch.get_num_threads()}", flush=True)
    t0 = time.time()
    trainer.train()
    print(f"trained in {(time.time() - t0) / 60:.1f} min", flush=True)


def choose_band(rows: list[dict], scores: list[float], threshold: float, precision: float = 0.95) -> tuple[float, float, dict]:
    """2단 판정 구간 [lo, hi] (상위 5건 쌍, train):
    - lo = 단일 임계값(threshold): 그 아래는 cross_encoder 모드와 똑같이 로컬 no. two_stage는 "로컬 yes 중 자신 없는 것만" API로 넘긴다.
    - hi: 점수 ≥ hi 인 쌍의 양성 비율(위에서부터 누적 precision) ≥ precision 이 되는 가장 낮은 점수 → 로컬 yes 확정.
    11-06 1차·2차 시도의 교훈: lo를 train 점수 분포로 잡으면(음성 누적 precision 0.95 → 0.86, 양성 5분위 → 0.72) 모델이 train 쌍을
    봤기 때문에 lo가 너무 높아진다(val 양성 22건이 lo 아래, positive false no_match 0.404). train 분포는 hi 쪽(음성이 위로 올라오는가)에만 쓴다."""
    ps = sorted((s, p["label"]) for p, s in zip(rows, scores) if p["rank"] <= TOP_K)
    n = len(ps)
    lo = threshold
    hi = ps[-1][0]
    pos_cum = 0
    for i, (s, y) in enumerate(reversed(ps), 1):
        pos_cum += y
        if pos_cum / i >= precision:
            hi = s
    if hi <= lo:
        hi = lo
    amb = sum(1 for s, _ in ps if lo < s < hi)
    return lo, hi, {"n_top5_pairs": n, "ambiguous_pairs": amb, "ambiguous_rate": round(amb / max(n, 1), 3), "precision_hi": precision}


def pick(args) -> None:
    """결과 중 val 목적함수 최대(동률이면 negative 처리율) → artifacts/reranker/best + manifest."""
    res = load_jsonl(RESULTS)
    if not res:
        raise SystemExit("no results")
    def obj(r):
        v = r["val"]; return (v["neg_no_match"] - v["false_no_match"] - v["dropped_rate"], v["neg_no_match"])
    best = max(res, key=obj)
    dst = ART / "best"
    if dst.exists():
        shutil.rmtree(dst)
    src = Path(best["model"])
    if src.exists():                                        # 추론에 필요한 것만(optimizer.pt 등 2GB는 제외)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("optimizer.pt", "scheduler.pt", "rng_state.pth",
                                                                "trainer_state.json", "training_args.bin"))
    else:                                                   # 제로샷(허브 모델)이 best면 그대로 저장
        from sentence_transformers import CrossEncoder
        CrossEncoder(best["model"], max_length=512).save(str(dst))
    sc = load_jsonl(ART / f"scores_{best['name']}.jsonl")
    tr = pairs("train")
    by = {(p["qid"], p["ticket_id"]): p for p in tr}
    rows = [by[(r["qid"], r["ticket_id"])] for r in sc if r["split"] == "train"]
    lo, hi, info = choose_band(rows, [r["score"] for r in sc if r["split"] == "train"], best["threshold"])
    (dst / "manifest.json").write_text(json.dumps({"name": best["name"], "source_model": best["model"], "threshold": best["threshold"],
                                                   "max_length": best.get("max_length", 512),
                                                   "band": [round(lo, 4), round(hi, 4)], "band_info": info,
                                                   "val": best["val"], "picked_at": time.strftime("%Y-%m-%dT%H:%M")},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    print("best:", best["name"], "threshold", best["threshold"], "band", (round(lo, 4), round(hi, 4)), info, "val", best["val"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["score", "train", "pick"])
    ap.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--name", default="zeroshot")
    ap.add_argument("--variant", choices=["all", "no_confusable"], default="all")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--device", default=None, help="cpu|mps (기본 auto). 학습은 cpu 권장 — MPS는 11-04 abort")
    ap.add_argument("--max-length", type=int, default=512, help="쌍 토큰 상한(쌍 평균 285·p95 444; 학습은 256)")
    args = ap.parse_args()
    global DEVICE, MAX_LENGTH
    DEVICE, MAX_LENGTH = args.device, args.max_length
    if args.cmd == "pick":
        pick(args); return
    tr, va = pairs("train"), pairs("val")
    if args.cmd == "score":
        evaluate(args.name, args.model, tr, va, extra={"variant": "zeroshot"})
    else:
        train(args, tr, va)


if __name__ == "__main__":
    main()
