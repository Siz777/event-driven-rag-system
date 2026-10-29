"""검색 + 생성 파이프라인. answer(InboundEvent) -> OutboundEvent 순수 함수.
ask.py(터미널)와 worker(큐)가 같은 함수를 쓴다. 검색·LLM은 주입 가능(테스트는 fake).

status 규칙
- 검색 결과 없음 / 최고 점수 < MIN_SCORE → no_match (LLM 호출 안 함)
- LLM이 티켓을 하나도 인용하지 않고 "유사 이력 없음"이라고 답함 → no_match
- 그 외 → answered, citations = 답변 본문에 등장한 티켓 ID 중 검색 결과에 있는 것(등장 순, 중복 제거, 검색 점수 포함)
- 예외(LLM 타임아웃·네트워크)는 삼키지 않는다 — 호출자(워커)가 재시도 경로로 보낸다.

MIN_SCORE는 쓰레기 걸러내기용 보조 신호(기본 0.45). negative 0.547~0.625와 positive 0.57~0.73이 겹치므로
임계값으로 "이력 없음"을 가르지 않는다 — 관련성 판정은 3주차 LLM 게이팅(docs/log 10-29 결정).
"""
from __future__ import annotations

import os
import re
from typing import Callable

from events import Citation, InboundEvent, OutboundEvent

from . import config
from .retrieve import Hit

MIN_SCORE = float(os.getenv("MIN_SCORE", "0.45"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
NO_MATCH = "유사 이력 없음"
_CITE = re.compile(r"INC-\d{4}-\d{4}")   # 대괄호 안 묶음 인용([INC-a, INC-b])도 잡히게 ID 자체를 찾는다

SYSTEM = """<role>
반도체 팹 설비 엔지니어의 보조. 과거 장애 티켓을 근거로 현재 증상에 대한 유사 이력과 확인 순서를 알려준다.
</role>

<context>
[과거 티켓]은 벡터 검색 상위 {k}건이며 전체 이력이 아니다. 건수를 말할 때는 "검색된 상위 {k}건 기준"이라고 명시한다.
각 티켓에는 증상·조치 기록·조치 결과·최종 원인이 있다.
</context>

<rules>
- [과거 티켓]만 근거로 답한다.
- 근거 티켓은 반드시 [INC-xxxx] 형태로 인용한다.
- 전체 400자 이내.
- 권장 확인 순서는 최대 4개.
</rules>

<output>
(1) 유사 사례 요약 (2) 과거 조치 내용 (3) 권장 확인 순서 — 세 항목, 간결하게.
</output>

티켓에 없는 내용은 추정하지 말고, 관련 티켓이 없으면 '유사 이력 없음'이라고 답하라."""

SearchFn = Callable[[str, int], list[Hit]]
ChatFn = Callable[[str, str], str]


def _default_search(query: str, k: int) -> list[Hit]:
    from .retrieve import search
    return search(query, k=k, method="vector")


def _default_chat(user: str, system: str) -> str:
    from .llm import chat
    return chat(user, system, timeout=LLM_TIMEOUT, max_attempts=2)   # 재시도는 워커의 retry 티어가 담당


def build_context(hits: list[Hit]) -> str:
    return "\n\n".join(
        f"[{h.ticket_id}] (유사도 {h.score:.2f})\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}"
        for h in hits
    )


def answer(inbound: InboundEvent, *, search: SearchFn = _default_search, chat: ChatFn = _default_chat,
           k: int = config.TOP_K, min_score: float = MIN_SCORE) -> OutboundEvent:
    hits = search(inbound.text, k)
    if not hits or hits[0].score < min_score:
        return inbound.reply(NO_MATCH, status="no_match")

    user = f"[질문]\n{inbound.text}\n\n[과거 티켓]\n{build_context(hits)}"
    text = chat(user, SYSTEM.format(k=len(hits))).strip()

    by_id = {h.ticket_id: h for h in hits}
    cited = []
    for tid in _CITE.findall(text):
        if tid in by_id and tid not in {c.ticket_id for c in cited}:
            cited.append(Citation(ticket_id=tid, score=round(by_id[tid].score, 4)))
    if not cited and NO_MATCH in text:
        return inbound.reply(text, status="no_match")
    return inbound.reply(text, citations=cited, status="answered")
