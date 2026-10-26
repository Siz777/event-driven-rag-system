"""생성 티켓 검증: (1) 역분류 — 증상만 보고 원인을 맞힐 수 있는가 (2) 근접 중복 제거.
사용: python -m scripts.validate_tickets
"""
import json
from collections import Counter

from tqdm import tqdm

from rag import config, embed
from rag.documents import doc_text, load_catalog, load_jsonl, save_jsonl
from rag.llm import chat_json

REVERSE = """아래는 반도체 설비 장애 티켓의 증상과 조치 기록입니다. 가장 가능성 높은 원인을 보기에서 하나 고르세요.

[티켓]
{text}

[보기]
{options}

JSON으로만 답하세요: {{"cause_id": "Cxx"}}"""


def main():
    cat = load_catalog()
    causes = {c["cause_id"]: c for c in cat["causes"]}
    tickets = load_jsonl(config.DATA_DIR / "tickets.jsonl")
    report = Counter()
    kept = []

    # 1) 역분류: 같은 설비 타입의 원인 8개 중 고르게 함 (confusable 쌍은 통과 = 의도한 난이도)
    for t in tqdm(tickets, desc="reverse-classify"):
        opts = [c for c in cat["causes"] if c["equipment_type"] == t["equipment_type"]]
        options = "\n".join(f"- {c['cause_id']}: {c['root_cause']}" for c in opts)
        text = f"증상: {t['symptom']}\n조치 기록: {t['action_log']}"
        try:
            pred = chat_json(REVERSE.format(text=text, options=options),
                             model=config.LLM_MODEL_SMALL, temperature=0)["cause_id"]
        except Exception:
            pred = "ERR"
        t["_reverse_pred"] = pred
        if pred == t["cause_id"] or pred in causes[t["cause_id"]].get("confusable_with", []):
            kept.append(t); report["pass"] += 1
        else:
            report["fail_reverse"] += 1

    # 2) 근접 중복: 같은 원인 내 코사인 0.95 이상이면 뒤의 것을 제거
    vecs = embed.embed([doc_text(t) for t in kept])
    by_cause: dict[str, list[int]] = {}
    for i, t in enumerate(kept):
        by_cause.setdefault(t["cause_id"], []).append(i)
    drop = set()
    for idxs in by_cause.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if j not in drop and float(vecs[i] @ vecs[j]) >= 0.95:
                    drop.add(j)
    clean = [t for i, t in enumerate(kept) if i not in drop]
    report["fail_duplicate"] = len(drop)

    save_jsonl(config.DATA_DIR / "tickets.clean.jsonl", clean)
    per_cause = Counter(t["cause_id"] for t in clean)
    with open(config.DATA_DIR / "validation_report.json", "w", encoding="utf-8") as f:
        json.dump({"summary": dict(report), "kept": len(clean), "per_cause": dict(sorted(per_cause.items()))},
                  f, ensure_ascii=False, indent=2)
    print(dict(report), "-> kept", len(clean))
    low = [k for k, v in per_cause.items() if v < 5]
    if low:
        print("원인당 5건 미만 → 보충 생성:", f"python -m scripts.generate_tickets --only {','.join(low)}")


if __name__ == "__main__":
    main()
