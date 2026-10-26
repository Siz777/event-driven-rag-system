"""평가셋 생성. 난이도별 질의 + 정답 티켓.
정답 정의: 같은 cause_id의 모든 티켓 ("이 장애 전에 있었나?" 의미). source_ticket_id는 MRR용.
사용: python -m scripts.build_evalset --per-difficulty 30
"""
import argparse
import random
from collections import defaultdict

from tqdm import tqdm

from rag import config
from rag.documents import load_catalog, load_tickets, save_jsonl
from rag.llm import chat_json

PROMPTS = {
    "exact": "아래 티켓 증상을 보고, 설비 엔지니어가 슬랙에 올릴 법한 짧은 질문 1개를 만드세요. 증상의 핵심 표현은 유지하되 문장으로 바꿉니다.",
    "paraphrase": "아래 티켓 증상을 보고, 같은 상황을 전혀 다른 어휘로 설명하는 질문 1개를 만드세요. 티켓에 나온 단어(명사)를 되도록 쓰지 말고, 현장에서 체감하는 방식으로 묘사합니다.",
    "multi_hop": "아래 티켓 증상을 보고, 과거 이력을 종합해야 답할 수 있는 질문 1개를 만드세요 (예: 이 증상의 평균 다운타임, 가장 흔한 조치, 어떤 설비에서 자주 났는지).",
}
NEGATIVE = """반도체 팹 설비 엔지니어가 할 법한 질문을 {n}개 만드세요. 단, 아래 [존재하는 원인 목록]에 해당하지 않는 상황이어야 합니다
(예: 목록에 없는 설비 종류, 목록에 없는 서브시스템 고장, 설비와 무관한 질문). 각 질문은 한 문장.

[존재하는 원인 목록]
{causes}

JSON 배열로만: ["질문1", "질문2", ...]"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-difficulty", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed)

    tickets = load_tickets()
    by_cause = defaultdict(list)
    for t in tickets:
        by_cause[t["cause_id"]].append(t["ticket_id"])
    rows = []

    for diff, instr in PROMPTS.items():
        sample = random.sample(tickets, min(args.per_difficulty, len(tickets)))
        for t in tqdm(sample, desc=diff):
            try:
                q = chat_json(f"{instr}\n\n[티켓 증상]\n{t['symptom']}\n\nJSON으로만: {{\"query\": \"...\"}}",
                              temperature=0.8)["query"]
            except Exception:
                continue
            rows.append({"query": q, "difficulty": diff, "source_ticket_id": t["ticket_id"],
                         "relevant_ticket_ids": by_cause[t["cause_id"]], "cause_id": t["cause_id"]})

    cat = load_catalog()
    causes = "\n".join(f"- {c['equipment_type']} / {c['subsystem']}: {c['root_cause']}" for c in cat["causes"])
    try:
        negs = chat_json(NEGATIVE.format(n=args.per_difficulty, causes=causes), temperature=0.8)
    except Exception:
        negs = []
    rows += [{"query": q, "difficulty": "negative", "source_ticket_id": "", "relevant_ticket_ids": [], "cause_id": ""}
             for q in negs]

    save_jsonl(config.EVAL_DIR / "eval.jsonl", rows)
    print(f"wrote {len(rows)} eval rows -> {config.EVAL_DIR / 'eval.jsonl'}")
    print("※ 손으로 쓴 질의 20~30개를 eval.jsonl에 추가하면 같은 LLM이 쓴 글끼리 매칭되는 편향이 줄어듭니다.")


if __name__ == "__main__":
    main()
