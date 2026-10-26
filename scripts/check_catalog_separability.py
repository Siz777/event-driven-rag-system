"""카탈로그 분리도 테스트: typical_symptoms만 보고 원인을 맞힐 수 있는가. 티켓 생성 전에 돌린다 (40회 호출).
사용: python -m scripts.check_catalog_separability
"""
from collections import Counter

from rag import config
from rag.documents import load_catalog
from rag.llm import chat_json
from scripts.validate_tickets import REVERSE


def main():
    cat = load_catalog()
    bad = Counter()
    for c in cat["causes"]:
        opts = [o for o in cat["causes"] if o["equipment_type"] == c["equipment_type"]]
        options = "\n".join(f"- {o['cause_id']}: {o['root_cause']}" for o in opts)
        text = "증상: " + ", ".join(c["typical_symptoms"])
        pred = chat_json(REVERSE.format(text=text, options=options), model=config.LLM_MODEL_SMALL, temperature=0)["cause_id"]
        ok = pred == c["cause_id"] or pred in c.get("confusable_with", [])
        mark = "ok " if ok else "XX "
        print(f"{mark}{c['cause_id']} -> {pred}  ({c['subsystem']}: {c['root_cause']})")
        if not ok:
            bad[(c["cause_id"], pred)] += 1
    if bad:
        print("\n카탈로그에서 구분 안 되는 쌍 (confusable_with 추가 또는 증상 차별화):", dict(bad))
    else:
        print("\n모든 원인이 분리됨")


if __name__ == "__main__":
    main()
