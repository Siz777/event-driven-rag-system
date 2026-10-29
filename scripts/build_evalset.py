"""평가셋 생성. 난이도별 질의 + 정답 티켓.
정답 정의: 같은 cause_id의 모든 티켓 ("이 장애 전에 있었나?" 의미). source_ticket_id는 MRR용.
사용: python -m scripts.build_evalset --per-difficulty 30   (--negatives-only: negative만 교체, --difficulty X --count N: X만 추가)
negative: hard(카탈로그 설비지만 목록 외 상황, 설비별 균등) + easy(타 설비·유틸리티·IT·안전), neg_type 필드로 구분.
"""
import argparse
import random
from collections import defaultdict

from tqdm import tqdm

from rag import config
from rag.documents import load_catalog, load_jsonl, load_tickets, save_jsonl
from rag.llm import chat_json

# 평가 질의 생성 프롬프트. <role>/<context>/<rules>/<output> 구조 (추론 모델용: 목표·제약 중심, 단계 지시 없음)
QUERY_TMPL = """<role>
반도체 팹 설비 엔지니어. 동료들이 쓰는 사내 슬랙 채널에 질문을 올린다.
</role>

<context>
아래 [티켓 증상]은 과거 장애 티켓의 증상 기록이다. 이 증상을 겪는 사람이 올릴 질문 1개를 만든다.
</context>

<rules>
{rules}
- 한 문장, 구어체. 설명문·교과서 문형 금지.
</rules>

<output>
JSON만: {{"query": "..."}}
</output>

[티켓 증상]
{symptom}"""

RULES = {
    "exact": "- 증상의 핵심 표현(용어·수치)은 그대로 유지하고 문장만 질문으로 바꾼다.",
    # 현실적 paraphrase: 용어는 유지, 문장만 다시 씀. (초기 버전의 "명사 쓰지 않기"는 수수께끼 질의를 만들어 paraphrase_hard로 분리)
    "paraphrase": "- 웨이퍼·챔버·압력·온도·척·He·펌프다운 등 현장 용어는 그대로 쓴다.\n- 티켓 문장·어순을 복사하지 말고, 오퍼레이터가 슬랙에 상황을 설명하듯 다시 쓴다.\n- 비유·완곡 표현 금지.",
    "paraphrase_hard": "- 티켓에 나온 명사를 되도록 쓰지 않는다.\n- 같은 상황을 현장에서 체감하는 방식(소리·시간·화면 표시·결과물)으로 묘사한다.",
    "multi_hop": "- 과거 이력을 종합해야 답할 수 있게 묻는다 (예: 이 증상의 평균 다운타임, 가장 흔한 조치, 어떤 설비에서 자주 났는지).\n- 증상 묘사는 짧게, 질문의 초점은 집계·패턴에 둔다.",
}
PROMPTS = {d: QUERY_TMPL.replace("{rules}", r) for d, r in RULES.items()}

NEGATIVE = """<role>
반도체 팹 설비 엔지니어. 사내 슬랙에 급하게 짧은 질문을 올린다.
</role>

<context>
아래 [존재하는 원인 목록]은 검색 시스템이 알고 있는 장애 원인 전부다. 이 목록 밖의 질문을 만들어 "해당 없음"을 판정하는 평가셋으로 쓴다.
hard: {equipment_list} 각 설비에서 {n_per_eq}개씩 총 {n_hard}개. 그 설비에서 실제로 생길 수 있는 **하드웨어** 상황이지만 목록의 원인·증상과 무관한 것. 다음 영역에서만 만든다(카탈로그에 없는 서브시스템): 로드포트·FOUP 클램프, 도어 인터락 스위치, N2 퍼지 라인, 배기 덕트·댐퍼, 장비 레벨링·캐스터, 케이블 트레이·커넥터 체결, 냉각 팬·필터(전장함), 조명·시그널 타워, 웨이퍼 카세트 슬롯 센서, 배관 피팅 체결(공정 가스 외).
easy: 총 {n_easy}개. 다른 설비(ALD, 이온주입, 퍼니스, 계측 등)·유틸리티(공조, 배기, 순수, 가스 공급)·IT(MES, 네트워크, 로그인)·안전(가스 누출 경보, 인터락 테스트).
</context>

<rules>
- 목록의 원인 어디에도 해당하지 않고, 목록의 증상과도 겹치지 않는다.
- hard는 증상을 기계적·물리적으로 쓴다(소리·움직임·체결·표시등·스위치 등). 목록의 증상(압력·유량·온도 편차·파티클·두께·CD·연마율 등)을 결과로 언급하지 않는다.
- 한 줄, 40자 이내, 구어체. 물음표 없어도 된다.
- "~할 수 있나요?", "~원인은 무엇인가요?" 같은 설명문·교과서 문형 금지.
- easy 15개는 로그/기록 문제에 몰리지 않게 주제를 고르게 섞는다.
- 문체 예시 (실제 질의, 내용은 따라 하지 말 것):
{style_examples}
</rules>

<output>
JSON만: {{"hard": [{{"equipment_type": "PECVD", "query": "..."}}, ...], "easy": ["질문1", ...]}}
</output>

[존재하는 원인 목록]
{causes}"""


def build_negatives(cat: dict, n: int, style_examples: list[str]) -> list[dict]:
    """hard n/2 (설비별 균등) + easy n/2. 실패 시 빈 리스트."""
    eqs = list(cat["equipment_types"])
    n_hard = n // 2
    n_per_eq = max(1, n_hard // len(eqs))
    causes = "\n".join(f"- {c['equipment_type']} / {c['subsystem']}: {c['root_cause']} (증상: {', '.join(c['typical_symptoms'])})"
                       for c in cat["causes"])
    prompt = NEGATIVE.format(n_hard=n_per_eq * len(eqs), n_easy=n - n_per_eq * len(eqs), n_per_eq=n_per_eq,
                             equipment_list="/".join(eqs), causes=causes,
                             style_examples="\n".join(f"- {q}" for q in style_examples))
    try:
        out = chat_json(prompt, temperature=0.8)
    except Exception:
        return []
    rows = []
    for h in out.get("hard", []):
        rows.append({"query": h["query"], "difficulty": "negative", "neg_type": "hard",
                     "equipment_type": h.get("equipment_type", ""), "source_ticket_id": "",
                     "relevant_ticket_ids": [], "cause_id": "", "source": "llm"})
    for q in out.get("easy", []):
        rows.append({"query": q, "difficulty": "negative", "neg_type": "easy", "equipment_type": "",
                     "source_ticket_id": "", "relevant_ticket_ids": [], "cause_id": "", "source": "llm"})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-difficulty", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--negatives-only", action="store_true",
                    help="기존 eval.jsonl의 negative 행만 교체 (positive 질의는 유지)")
    ap.add_argument("--hard-only", action="store_true", help="--negatives-only와 함께: hard만 교체, easy 유지")
    ap.add_argument("--difficulty", choices=list(RULES), help="이 난이도만 생성해 기존 eval.jsonl에 추가")
    ap.add_argument("--count", type=int, default=None, help="--difficulty와 함께: 생성 수 (기본 --per-difficulty)")
    args = ap.parse_args()
    random.seed(args.seed)
    cat = load_catalog()
    eval_path = config.EVAL_DIR / "eval.jsonl"

    if args.difficulty:
        rows = load_jsonl(eval_path) if eval_path.exists() else []
        tickets = load_tickets()
        by_cause = defaultdict(list)
        for t in tickets:
            by_cause[t["cause_id"]].append(t["ticket_id"])
        n = args.count or args.per_difficulty
        used = {r["source_ticket_id"] for r in rows if r["difficulty"] == args.difficulty}
        pool = [t for t in tickets if t["ticket_id"] not in used]
        added = 0
        for t in tqdm(random.sample(pool, min(n, len(pool))), desc=args.difficulty):
            try:
                q = chat_json(PROMPTS[args.difficulty].format(symptom=t["symptom"]), temperature=0.8)["query"]
            except Exception:
                continue
            rows.append({"query": q, "difficulty": args.difficulty, "source_ticket_id": t["ticket_id"],
                         "relevant_ticket_ids": by_cause[t["cause_id"]], "cause_id": t["cause_id"], "source": "llm"})
            added += 1
        save_jsonl(eval_path, rows)
        print(f"added {added} {args.difficulty} rows -> {len(rows)} total")
        return

    if args.negatives_only:
        old = load_jsonl(eval_path)
        keep_easy = [r for r in old if r["difficulty"] == "negative" and r.get("neg_type") == "easy"] if args.hard_only else []
        rows = [r for r in old if r["difficulty"] != "negative"]
        style = [r["query"] for r in rows if r["difficulty"] == "paraphrase"][:5]
        negs = build_negatives(cat, args.per_difficulty, style)
        if args.hard_only:
            negs = [r for r in negs if r["neg_type"] == "hard"] + keep_easy
        save_jsonl(eval_path, rows + negs)
        print(f"negatives replaced: {len(negs)} (hard {sum(r['neg_type'] == 'hard' for r in negs)}, "
              f"easy {sum(r['neg_type'] == 'easy' for r in negs)}) -> {len(rows) + len(negs)} rows")
        return

    tickets = load_tickets()
    by_cause = defaultdict(list)
    for t in tickets:
        by_cause[t["cause_id"]].append(t["ticket_id"])
    rows = []

    for diff, instr in PROMPTS.items():
        if diff == "paraphrase_hard":          # 기본 생성은 exact/paraphrase/multi_hop. hard는 --difficulty로 별도
            continue
        sample = random.sample(tickets, min(args.per_difficulty, len(tickets)))
        for t in tqdm(sample, desc=diff):
            try:
                q = chat_json(instr.format(symptom=t["symptom"]), temperature=0.8)["query"]
            except Exception:
                continue
            rows.append({"query": q, "difficulty": diff, "source_ticket_id": t["ticket_id"],
                         "relevant_ticket_ids": by_cause[t["cause_id"]], "cause_id": t["cause_id"], "source": "llm"})

    style = [r["query"] for r in rows if r["difficulty"] == "paraphrase"][:5]
    rows += build_negatives(cat, args.per_difficulty, style)

    save_jsonl(eval_path, rows)
    print(f"wrote {len(rows)} eval rows -> {config.EVAL_DIR / 'eval.jsonl'}")
    print("※ 손으로 쓴 질의 20~30개를 eval.jsonl에 추가하면 같은 LLM이 쓴 글끼리 매칭되는 편향이 줄어듭니다.")


if __name__ == "__main__":
    main()
