"""카탈로그 분리도 테스트: typical_symptoms만 보고 원인을 맞힐 수 있는가. 티켓 생성 전에 돌린다 (40회 호출, JUDGE_* 사용).
사용: python -m scripts.check_catalog_separability [--runs 2]
--runs N: 보기 순서를 바꿔 N번 반복, 다수결. 소형 심판의 노이즈를 줄인다.
"""
import argparse
from collections import Counter

from rag.documents import load_catalog
from rag.judge import classify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    cat = load_catalog()
    exact = via = 0
    bad = Counter()
    for c in cat["causes"]:
        opts = [o for o in cat["causes"] if o["equipment_type"] == c["equipment_type"]]
        text = "증상: " + ", ".join(c["typical_symptoms"])
        votes = Counter(classify(text, opts, seed=r) for r in range(args.runs))
        pred = votes.most_common(1)[0][0]
        if pred == c["cause_id"]:
            exact += 1; mark = "ok "
        elif pred in c.get("confusable_with", []):
            via += 1; mark = "~  "
        else:
            bad[(c["cause_id"], pred)] += 1; mark = "XX "
        extra = f"  votes={dict(votes)}" if args.runs > 1 else ""
        print(f"{mark}{c['cause_id']} -> {pred}  ({c['subsystem']}: {c['root_cause']}){extra}")
    n = len(cat["causes"])
    print(f"\n정확 {exact}/{n}, confusable 경유 {via}/{n}, 실패 {len(bad)}/{n}")
    if bad:
        print("구분 안 되는 쌍 (심판 교체/반복 후에도 남으면 카탈로그 수정):", dict(bad))


if __name__ == "__main__":
    main()
