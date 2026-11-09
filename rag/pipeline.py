"""검색 + 생성 파이프라인. answer(InboundEvent) -> OutboundEvent 순수 함수.
ask.py(터미널)와 worker(큐)가 같은 함수를 쓴다. 3주차 Step 4부터 본체는 rag/graph.py(LangGraph)이고 여기는 호출 껍데기 —
시그니처(search/chat 주입, k, min_score)는 유지한다. 계약·워커는 바뀌지 않는다.

status 규칙 (graph.py 참조)
- 검색 결과 없음 → no_match (LLM 호출 안 함, 본문 NO_MATCH)
- 벡터 top1 cosine < min_score(기본 0.50) → no_match (LLM 호출 안 함)
- LLM 관련성 판정 yes 0건 → no_match
- 생성 답변에 검색 결과 티켓 인용 0건 → no_match
- 판정 통과 티켓이 설비 2종 이상에 걸치고 질의에 설비·코드가 없음 → clarify (설비 되묻기, Step 9; allow_clarify=False면 건너뜀)
- 그 외 → answered, citations = 답변 본문에 등장한 티켓 ID 중 판정 통과 결과에 있는 것(등장 순, 중복 제거, cosine)
- 예외(LLM 타임아웃·네트워크)는 삼키지 않는다 — 호출자(워커)가 재시도 경로로 보낸다.
"""
from __future__ import annotations

from events import InboundEvent, OutboundEvent

from . import config
from .graph import CLARIFY, GRADE_MODE, MIN_COSINE, NO_MATCH, SYSTEM, TOOLS, ChatFn, GradeBatchFn, GradeFn, SearchFn  # noqa: F401
from .graph import build_context, default_chat, default_grade, default_grade_batch, default_grade_cross_encoder, default_scores_cross_encoder, default_search, default_tool_chat, default_tool_exec  # noqa: F401
from .graph import run as _run

MIN_SCORE = MIN_COSINE


def answer(inbound: InboundEvent, *, search: SearchFn = default_search, chat: ChatFn = default_chat,
           grade: GradeFn = default_grade, grade_batch: GradeBatchFn = default_grade_batch, grade_mode: str = GRADE_MODE,
           k: int = config.TOP_K, min_score: float = MIN_SCORE, trace: dict | None = None,
           clarify: bool = CLARIFY, allow_clarify: bool = True, grade_cross=default_grade_cross_encoder,
           cross_scores=default_scores_cross_encoder, band: tuple[float, float] | None = None,
           tools: bool = TOOLS, tool_chat=default_tool_chat, tool_exec=default_tool_exec) -> OutboundEvent:
    return _run(inbound, k=k, trace=trace, allow_clarify=allow_clarify, search=search, chat=chat, grade=grade,
                grade_batch=grade_batch, grade_cross=grade_cross, cross_scores=cross_scores, band=band,
                grade_mode=grade_mode, min_cosine=min_score, clarify=clarify, tools=tools, tool_chat=tool_chat, tool_exec=tool_exec)
