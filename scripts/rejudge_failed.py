"""2차 심판: 1차(JUDGE_*, 로컬 7B) 역분류에서 탈락한 티켓만 생성 모델(LLM_*)로 다시 판정한다.
7B 심판은 설비별로 특정 원인에 쏠리는 경향이 있어(PHOTO→C28 등) 탈락을 바로 버리지 않는다.
사용: python -m scripts.rejudge_failed
입력: tickets.jsonl, tickets.clean.jsonl(1차 통과), validation_report.json(1차 결과)
출력: tickets.jsonl에 _reverse_pred/_reverse_pred_2 기록, tickets.clean.jsonl = 1차 통과 + 2차 복원(중복 제거 재실행),
      validation_report.json에 judge1/judge2/judge_bias/final 섹션
"""
import json
from collections import Counter

from tqdm import tqdm

from rag import config
from rag.documents import load_catalog, load_jsonl, save_jsonl
from rag.judge import classify
from scripts.validate_tickets import dedupe

BIAS_TARGETS = ["C28", "C17", "C12", "C09"]   # 1차 심판이 설비별로 쏠린 원인 (관찰 기준)


def passes(pred: str, cause: dict) -> bool:
    return pred == cause["cause_id"] or pred in cause.get("confusable_with", [])


def main():
    cat = load_catalog()
    causes = {c["cause_id"]: c for c in cat["causes"]}
    tickets = load_jsonl(config.DATA_DIR / "tickets.jsonl")
    clean1 = load_jsonl(config.DATA_DIR / "tickets.clean.jsonl")
    report1 = json.load(open(config.DATA_DIR / "validation_report.json", encoding="utf-8"))
    if "judge1" in report1:                      # 이미 2차까지 돌린 report면 1차 부분만 쓴다
        report1 = report1["judge1"]

    pred1 = {t["ticket_id"]: t["_reverse_pred"] for t in clean1}
    pred1.update({f["ticket_id"]: f["pred"] for f in report1["failed"]})
    failed_ids = {f["ticket_id"] for f in report1["failed"]}
    for t in tickets:
        t["_reverse_pred"] = pred1.get(t["ticket_id"], "")
    print(f"1차 통과 {len(clean1)}, 1차 탈락 {len(failed_ids)} → 2차 심판 {config.LLM_MODEL}")

    # 2차 심판 (탈락분만)
    restored, still = [], []
    for t in tqdm([t for t in tickets if t["ticket_id"] in failed_ids], desc="rejudge"):
        opts = [c for c in cat["causes"] if c["equipment_type"] == t["equipment_type"]]
        text = f"증상: {t['symptom']}\n조치 기록: {t['action_log']}"
        t["_reverse_pred_2"] = classify(text, opts, provider="llm")
        (restored if passes(t["_reverse_pred_2"], causes[t["cause_id"]]) else still).append(t)
    save_jsonl(config.DATA_DIR / "tickets.jsonl", tickets)

    # clean 재생성: 1차 통과(최신 본문 사용) + 2차 복원, 중복 제거 재실행
    by_id = {t["ticket_id"]: t for t in tickets}
    kept = [by_id[t["ticket_id"]] for t in clean1] + restored
    kept.sort(key=lambda t: t["ticket_id"])
    clean, n_dup = dedupe(kept)
    save_jsonl(config.DATA_DIR / "tickets.clean.jsonl", clean)

    # 1차 심판 쏠림: 탈락 티켓의 1차 예측이 BIAS_TARGETS에 몰린 정도 (설비·정답 원인별)
    bias = {}
    for target in BIAS_TARGETS:
        hit = [f for f in report1["failed"] if f["pred"] == target]
        bias[target] = {"n": len(hit),
                        "equipment_type": causes[target]["equipment_type"],
                        "from_cause": dict(Counter(f["cause_id"] for f in hit))}
    pred1_dist = Counter(f["pred"] for f in report1["failed"])

    per_cause = Counter(t["cause_id"] for t in clean)
    report = {
        "judge1": {"model": config.JUDGE_MODEL, **report1},
        "judge2": {"model": config.LLM_MODEL, "rejudged": len(failed_ids), "restored": len(restored),
                   "still_failed": len(still),
                   "restored_ids": [t["ticket_id"] for t in restored],
                   "failed": [{"ticket_id": t["ticket_id"], "cause_id": t["cause_id"], "pred1": t["_reverse_pred"],
                               "pred2": t["_reverse_pred_2"], "symptom": t["symptom"]} for t in still]},
        "judge_bias": {"note": "1차 탈락 90건에서 심판이 고른 원인의 분포. BIAS_TARGETS는 관찰로 지정",
                       "pred1_distribution": dict(pred1_dist.most_common()), **bias},
        "final": {"summary": {"pass_judge1": len(clean1), "restored_judge2": len(restored),
                              "fail_both": len(still), "fail_duplicate": n_dup},
                  "kept": len(clean), "per_cause": dict(sorted(per_cause.items()))},
    }
    with open(config.DATA_DIR / "validation_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n복원 {len(restored)}, 여전히 탈락 {len(still)}, 중복 제거 {n_dup} → kept {len(clean)}/{len(tickets)}")
    print("원인별 최종 건수:", dict(sorted(per_cause.items())))
    low = [k for k, v in per_cause.items() if v < 5]
    if low:
        print("원인당 5건 미만:", ",".join(low))
    if still:
        print("\n여전히 탈락 (1차→2차 예측):")
        for t in still:
            print(f"  [{t['ticket_id']} {t['cause_id']} → {t['_reverse_pred']} / {t['_reverse_pred_2']}] {t['symptom']}")


if __name__ == "__main__":
    main()
