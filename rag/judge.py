"""역분류 심판. 보기 순서 셔플(위치 편향 제거). 프롬프트는 모델 등급별(7B / 추론 모델)로 분리."""
import random

from .llm import chat, judge_chat_json, parse_json

# 프롬프트를 모델 등급별로 나눈다.
# - 7B(Ollama, provider="judge"): "각 보기를 한 줄씩 비교한 뒤 고르라"는 추론 단계를 명시해야 정확도가 오른다
#   (분리도 테스트에서 단순 선택보다 안정적). reasoning 필드를 먼저 쓰게 해 답을 뒤로 미룬다.
# - 추론 모델(gpt-5 계열, provider="llm"): 내부에서 이미 추론하므로 단계 지시는 불필요하고 출력만 길어진다.
#   역할·제약·출력 형식만 주고(태그 구조), reasoning은 받지 않는다.
PROMPT_SMALL = """아래는 반도체 설비 장애 티켓의 증상과 조치 기록입니다.
보기 중 가장 가능성 높은 원인 하나를 고르세요.

[티켓]
{text}

[보기]
{options}

먼저 각 보기가 티켓의 증상과 맞는지 한 줄씩 짧게 비교한 뒤(reasoning), 가장 잘 맞는 보기의 cause_id를 고르세요.
JSON으로만 답하세요: {{"reasoning": "...", "cause_id": "Cxx"}}"""

PROMPT_LLM = """<role>
반도체 설비 장애 티켓을 읽고 근본 원인을 분류하는 심판.
</role>

<context>
[티켓]은 증상과 조치 기록만 담고 있고 원인은 지워져 있다. [보기]는 같은 설비 타입의 원인 후보다.
</context>

<rules>
- 증상·조치 기록에 직접 드러난 단서만 근거로 고른다.
- 보기 중 정확히 하나를 고른다. 보기 밖의 cause_id는 쓰지 않는다.
</rules>

<output>
JSON만: {{"cause_id": "Cxx"}}
</output>

[티켓]
{text}

[보기]
{options}"""


def classify(text: str, candidates: list[dict], seed: int | None = None, *, provider: str = "judge") -> str:
    """candidates: [{cause_id, root_cause}, ...] — 호출마다 순서를 섞어 제시.
    provider: "judge" = JUDGE_*(기본, 로컬 7B), "llm" = LLM_*(생성 모델, 2차 심판용)."""
    opts = list(candidates)
    random.Random(seed).shuffle(opts)
    options = "\n".join(f"- {o['cause_id']}: {o['root_cause']}" for o in opts)
    try:
        if provider == "judge":
            out = judge_chat_json(PROMPT_SMALL.format(text=text, options=options))
        else:
            out = parse_json(chat(PROMPT_LLM.format(text=text, options=options), temperature=0.0))
        cid = str(out.get("cause_id", "")).strip()
        return cid if cid in {o["cause_id"] for o in opts} else "ERR"
    except Exception:
        return "ERR"
