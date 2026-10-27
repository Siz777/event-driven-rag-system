"""역분류 심판. 보기 순서 셔플(위치 편향 제거) + 짧은 비교 추론 후 답(소형 모델 정확도 향상)."""
import random

from .llm import chat, judge_chat_json, parse_json

PROMPT = """아래는 반도체 설비 장애 티켓의 증상과 조치 기록입니다.
보기 중 가장 가능성 높은 원인 하나를 고르세요.

[티켓]
{text}

[보기]
{options}

먼저 각 보기가 티켓의 증상과 맞는지 한 줄씩 짧게 비교한 뒤(reasoning), 가장 잘 맞는 보기의 cause_id를 고르세요.
JSON으로만 답하세요: {{"reasoning": "...", "cause_id": "Cxx"}}"""


def classify(text: str, candidates: list[dict], seed: int | None = None, *, provider: str = "judge") -> str:
    """candidates: [{cause_id, root_cause}, ...] — 호출마다 순서를 섞어 제시.
    provider: "judge" = JUDGE_*(기본, 로컬 7B), "llm" = LLM_*(생성 모델, 2차 심판용)."""
    opts = list(candidates)
    random.Random(seed).shuffle(opts)
    options = "\n".join(f"- {o['cause_id']}: {o['root_cause']}" for o in opts)
    prompt = PROMPT.format(text=text, options=options)
    try:
        out = judge_chat_json(prompt) if provider == "judge" else parse_json(chat(prompt, temperature=0.0))
        cid = str(out.get("cause_id", "")).strip()
        return cid if cid in {o["cause_id"] for o in opts} else "ERR"
    except Exception:
        return "ERR"
