"""생성 티켓 검증: (1) 역분류 — 증상만 보고 원인을 맞힐 수 있는가 (2) 근접 중복 제거 (3) 타 설비 어휘 경고(탈락 아님).
사용: python -m scripts.validate_tickets
"""
import json
import re
from collections import Counter

from tqdm import tqdm

from rag import config, embed
from rag.documents import doc_text, load_catalog, load_jsonl, save_jsonl
from rag.judge import classify


# 대기압 장비(PHOTO·CMP)에 진공 계열 어휘가 들어가면 경고만 기록 (_warn). 프롬프트 v2 vocab 규칙 준수 확인용
FORBIDDEN = {"PHOTO": r"mTorr|sccm|TMP|MFC|진공", "CMP": r"mTorr|sccm|TMP|MFC|진공"}


def vocab_warnings(t: dict) -> list[str]:
    pat = FORBIDDEN.get(t["equipment_type"])
    if not pat:
        return []
    out = []
    for f in ("symptom", "action_log", "resolution_note"):
        words = sorted(set(re.findall(pat, t[f])))
        if words:
            out.append(f"{f}: {', '.join(words)}")
    return out


def dedupe(kept: list[dict], threshold: float = 0.95) -> tuple[list[dict], int]:
    """같은 원인 내 코사인 threshold 이상이면 뒤의 것을 제거. (clean, 제거 수)"""
    vecs = embed.embed([doc_text(t) for t in kept])
    by_cause: dict[str, list[int]] = {}
    for i, t in enumerate(kept):
        by_cause.setdefault(t["cause_id"], []).append(i)
    drop = set()
    for idxs in by_cause.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if j not in drop and float(vecs[i] @ vecs[j]) >= threshold:
                    drop.add(j)
    return [t for i, t in enumerate(kept) if i not in drop], len(drop)


def main():
    cat = load_catalog()
    causes = {c["cause_id"]: c for c in cat["causes"]}
    tickets = load_jsonl(config.DATA_DIR / "tickets.jsonl")
    report = Counter()
    kept, failed = [], []

    # 1) 역분류: 같은 설비 타입의 원인 8개 중 고르게 함 (confusable 쌍은 통과 = 의도한 난이도)
    for t in tqdm(tickets, desc="reverse-classify"):
        opts = [c for c in cat["causes"] if c["equipment_type"] == t["equipment_type"]]
        text = f"증상: {t['symptom']}\n조치 기록: {t['action_log']}"
        pred = classify(text, opts)
        t["_reverse_pred"] = pred
        warn = vocab_warnings(t)
        if warn:
            t["_warn"] = warn; report["warn_vocab"] += 1
        if pred == t["cause_id"]:
            kept.append(t); report["pass"] += 1
        elif pred in causes[t["cause_id"]].get("confusable_with", []):
            kept.append(t); report["pass"] += 1; report["pass_via_confusable"] += 1
        else:
            report["fail_reverse"] += 1; failed.append(t)

    # 2) 근접 중복 제거
    clean, n_dup = dedupe(kept)
    report["fail_duplicate"] = n_dup

    save_jsonl(config.DATA_DIR / "tickets.clean.jsonl", clean)
    per_cause = Counter(t["cause_id"] for t in clean)
    fail_per_cause = Counter(t["cause_id"] for t in failed)
    with open(config.DATA_DIR / "validation_report.json", "w", encoding="utf-8") as f:
        json.dump({"summary": dict(report), "kept": len(clean), "per_cause": dict(sorted(per_cause.items())),
                   "fail_per_cause": dict(sorted(fail_per_cause.items())),
                   "failed": [{"ticket_id": t["ticket_id"], "cause_id": t["cause_id"], "pred": t["_reverse_pred"],
                               "symptom": t["symptom"]} for t in failed],
                   "warnings": [{"ticket_id": t["ticket_id"], "cause_id": t["cause_id"], "warn": t["_warn"]}
                                for t in kept if t.get("_warn")]},
                  f, ensure_ascii=False, indent=2)
    print(dict(report), "-> kept", len(clean))
    low = [k for k, v in per_cause.items() if v < 5]
    if low:
        print("원인당 5건 미만 → 보충 생성:", f"python -m scripts.generate_tickets --only {','.join(low)}")


if __name__ == "__main__":
    main()
