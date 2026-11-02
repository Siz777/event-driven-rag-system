"""LangGraph 답변 그래프 (3주차 Step 4): retrieve → grade → (generate | no_answer) → respond.

grade 2단
 (a) cosine 보조: 벡터 top1 cosine < MIN_COSINE(0.50)이면 LLM 없이 no_answer. positive 최저 0.535(11-02 측정)보다 낮은 안전선이라
     negative 일부만 걸린다. 코드 분기(BM25 순위만, cosine 없음)는 이 단계를 건너뛴다.
 (b) LLM 판정: 상위 k건 각각 "이 티켓이 질문과 같은 설비·증상·원인인가" yes/no (LLM_MODEL_SMALL, 스레드 병렬).
     yes가 MIN_YES(2)건 미만이면 no_answer. 1건만 yes인 경우는 11-02 평가에서 negative 4건(소음·커넥터·냄새처럼 느슨하게 겹치는 티켓
     하나)뿐이었고 positive는 0건 — 5건 중 1건만 yes면 근거가 아니라 우연으로 본다.
generate 후검증: 답변에 검색 결과 티켓 인용이 0건이면 no_answer(근거 없는 답은 내보내지 않는다).
no_answer 본문: "검색 상위 k건 중 판정된 티켓 없음" + 가장 가까운 티켓 1건 "참고(유사도 낮음)" + 신규 티켓 등록 안내.
  본문이 "유사 이력 없음"으로 시작하지 않는 이유: Slack 어댑터(format_reply)가 그 문구로 시작하는 본문을 통째로 버리고 자기 문구만 보낸다.
  status=no_match이면 어댑터가 머리말을 붙이므로 본문은 상세만 담는다.

검색·LLM·판정은 주입 가능(테스트는 fake). 예외는 삼키지 않는다 — 워커 재시도 경로가 처리.
state.timings/llm_calls는 평가(scripts/eval_graph.py)용 계측.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from events import Citation, InboundEvent, OutboundEvent

from . import config
from .retrieve import Hit

MIN_COSINE = float(os.getenv("GRADE_MIN_COSINE", "0.50"))
MIN_YES = int(os.getenv("GRADE_MIN_YES", "2"))
GRADE_MODE = os.getenv("GRADE_MODE", "per_doc")      # per_doc: 티켓별 호출(병렬) / batch: 티켓 k건을 한 호출로 판정
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
GRADE_TIMEOUT = float(os.getenv("GRADE_TIMEOUT", "30"))
NO_MATCH = "유사 이력 없음"
_CITE = re.compile(r"INC-\d{4}-\d{4}")   # 대괄호 안 묶음 인용([INC-a, INC-b])도 잡히게 ID 자체를 찾는다

SYSTEM = """<role>
반도체 팹 설비 엔지니어의 보조. 과거 장애 티켓을 근거로 현재 증상에 대한 유사 이력과 확인 순서를 알려준다.
</role>

<context>
[과거 티켓]은 검색 상위 {k}건 중 관련성 판정을 통과한 것이며 전체 이력이 아니다. 건수를 말할 때는 "검색된 상위 {k}건 기준"이라고 명시한다.
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

GRADE_SYSTEM = """<role>
반도체 설비 장애 이력 검색 결과의 관련성 심판.
</role>

<context>
[질문]은 현장 엔지니어가 지금 겪는 증상 문의(또는 그 증상의 과거 이력 집계 요청)이고, [티켓]은 검색된 과거 장애 1건이다.
</context>

<rules>
- 티켓이 질문과 같은 종류의 문제(같은 설비 계열에서 같은 계열의 증상 또는 같은 원인)를 다루면 yes, 다른 문제면 no.
- 질문이 설비 타입이나 알람 코드를 특정하면 다른 설비 타입·다른 코드의 티켓은 no.
- 질문이 평균 다운타임·빈도 같은 집계를 물으면, 그 증상 계열의 티켓이면 yes.
- 단어가 겹친다는 이유만으로 yes 하지 않는다. 증상이 현장 체감 표현(소리·시간·화면)으로 쓰였으면 그 의미를 해석해 판단한다.
</rules>

<output>
JSON만: {"relevant": "yes" 또는 "no"}
</output>"""

GRADE_BATCH_SYSTEM = GRADE_SYSTEM.replace("[티켓]은 검색된 과거 장애 1건이다.", "[티켓 목록]은 검색된 과거 장애 여러 건이며 각각 [INC-…] ID가 붙어 있다.") \
    .replace("""<output>
JSON만: {"relevant": "yes" 또는 "no"}
</output>""", """<output>
티켓마다 독립적으로 판단한다. JSON만: {"relevant": ["INC-…", …]} — yes인 티켓 ID 목록(없으면 빈 목록).
</output>""")

SearchFn = Callable[[str, int], list[Hit]]
ChatFn = Callable[[str, str], str]
GradeFn = Callable[[str, Hit], bool]
GradeBatchFn = Callable[[str, list[Hit]], list[bool]]


class State(TypedDict, total=False):
    question: str
    k: int
    hits: list[Hit]
    vector_top1: float | None
    route: str                 # retrieve 분기(code/type/none…)
    graded: list[bool]
    kept: list[Hit]
    gate: str                  # cosine / llm / pass / empty
    text: str
    citations: list[Citation]
    status: str
    llm_calls: int
    timings: dict[str, float]


# ---- 기본 구현 (실제 검색·LLM) ----
def default_search(query: str, k: int) -> list[Hit]:
    from .retrieve import search
    return search(query, k=k, method="hybrid+filter")


def default_chat(user: str, system: str) -> str:
    from .llm import chat
    return chat(user, system, timeout=LLM_TIMEOUT, max_attempts=2)   # 재시도는 워커의 retry 티어가 담당


def default_grade(question: str, hit: Hit) -> bool:
    from .llm import chat, parse_json
    user = f"[질문]\n{question}\n\n[티켓]\n{hit.payload['_text']}\n최종 원인: {hit.payload['root_cause']}"
    raw = chat(user, GRADE_SYSTEM, model=config.LLM_MODEL_SMALL, temperature=0.0, timeout=GRADE_TIMEOUT, max_attempts=2)
    try:
        return str(parse_json(raw).get("relevant", "")).strip().lower().startswith("y")
    except Exception:
        return "yes" in raw.lower() and "no" not in raw.lower()


def default_grade_batch(question: str, hits: list[Hit]) -> list[bool]:
    """k건을 한 번에 판정. 출력은 yes 티켓 ID 목록. 파싱 실패 시 본문에서 ID를 정규식으로 줍는다."""
    from .llm import chat, parse_json
    body = "\n\n".join(f"[{h.ticket_id}]\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}" for h in hits)
    user = f"[질문]\n{question}\n\n[티켓 목록]\n{body}"
    raw = chat(user, GRADE_BATCH_SYSTEM, model=config.LLM_MODEL_SMALL, temperature=0.0, timeout=GRADE_TIMEOUT, max_attempts=2)
    try:
        yes = {str(x) for x in parse_json(raw).get("relevant", [])}
    except Exception:
        yes = set(_CITE.findall(raw))
    return [h.ticket_id in yes for h in hits]


def _sim(h: Hit) -> str:
    return f" (유사도 {h.cosine:.2f})" if h.cosine is not None else ""


def build_context(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{h.ticket_id}]{_sim(h)}\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}" for h in hits)


def no_answer_text(k: int, hits: list[Hit]) -> str:
    lines = [f"검색된 상위 {k}건 중 질문과 같은 설비·증상으로 판정된 티켓이 없습니다."]
    if hits:
        h = hits[0]
        p = h.payload
        symptom = (p.get("symptom") or "").replace("\n", " ")[:80]
        lines.append(f"참고(유사도 낮음): [{h.ticket_id}] {p.get('equipment_id', '')} · {symptom}{_sim(h)}")
    lines.append("처음 보는 증상이면 신규 티켓으로 등록해 주세요(설비명·알람 코드·증상·발생 시각). 설비명이나 알람 코드를 넣어 다시 물으면 검색 범위가 좁아집니다.")
    return "\n".join(lines)


# ---- 그래프 ----
def build_graph(*, search: SearchFn = default_search, chat: ChatFn = default_chat, grade: GradeFn = default_grade,
                grade_batch: GradeBatchFn = default_grade_batch, grade_mode: str = GRADE_MODE,
                min_cosine: float = MIN_COSINE, min_yes: int = MIN_YES, parallel: int = 5):
    if grade_mode not in ("per_doc", "batch"):
        raise ValueError(f"unknown GRADE_MODE: {grade_mode}")
    def retrieve(st: State) -> dict:
        t0 = time.monotonic()
        hits = search(st["question"], st["k"])
        top1 = next((h.cosine for h in hits if h.cosine is not None), None) if hits else None
        # hybrid+filter는 RRF 순이라 hits[0]의 cosine이 벡터 1위가 아닐 수 있다 → 벡터 cosine 최댓값
        if hits:
            cs = [h.cosine for h in hits if h.cosine is not None]
            top1 = max(cs) if cs else None
        return {"hits": hits, "vector_top1": top1, "llm_calls": 0,
                "timings": {**st.get("timings", {}), "retrieve": time.monotonic() - t0}}

    def grade_node(st: State) -> dict:
        t0 = time.monotonic()
        hits = st["hits"]
        if not hits:
            return {"gate": "empty", "graded": [], "kept": [], "timings": {**st["timings"], "grade": 0.0}}
        if st["vector_top1"] is not None and st["vector_top1"] < min_cosine:
            return {"gate": "cosine", "graded": [False] * len(hits), "kept": [],
                    "timings": {**st["timings"], "grade": time.monotonic() - t0}}
        if grade_mode == "batch":
            graded = [bool(x) for x in grade_batch(st["question"], hits)]
            calls = 1
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(hits)))) as ex:
                graded = list(ex.map(lambda h: bool(grade(st["question"], h)), hits))
            calls = len(hits)
        kept = [h for h, ok in zip(hits, graded) if ok]
        if len(kept) < min(min_yes, len(hits)):          # 검색 결과가 min_yes보다 적으면 전부 yes일 때만 통과
            kept = []
        return {"gate": "llm" if not kept else "pass", "graded": graded, "kept": kept,
                "llm_calls": st["llm_calls"] + calls, "timings": {**st["timings"], "grade": time.monotonic() - t0}}

    def route(st: State) -> str:
        return "generate" if st["kept"] else "no_answer"

    def generate(st: State) -> dict:
        t0 = time.monotonic()
        kept = st["kept"]
        user = f"[질문]\n{st['question']}\n\n[과거 티켓]\n{build_context(kept)}"
        text = chat(user, SYSTEM.format(k=st["k"])).strip()
        by_id = {h.ticket_id: h for h in kept}
        cited: list[Citation] = []
        for tid in _CITE.findall(text):
            if tid in by_id and tid not in {c.ticket_id for c in cited}:
                c = by_id[tid].cosine
                cited.append(Citation(ticket_id=tid, score=round(c, 4) if c is not None else None))
        out = {"llm_calls": st["llm_calls"] + 1, "timings": {**st["timings"], "generate": time.monotonic() - t0}}
        if not cited:                                     # 후검증: 인용 0건 → no_answer (근거 없는 답은 내보내지 않는다)
            return {**out, "gate": "no_citation", "status": "no_match", "citations": [],
                    "text": no_answer_text(st["k"], st["hits"])}
        return {**out, "status": "answered", "citations": cited, "text": text}

    def no_answer(st: State) -> dict:
        return {"status": "no_match", "citations": [], "text": no_answer_text(st["k"], st["hits"])}

    g = StateGraph(State)
    g.add_node("retrieve", retrieve)
    g.add_node("grade", grade_node)
    g.add_node("generate", generate)
    g.add_node("no_answer", no_answer)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", route, {"generate": "generate", "no_answer": "no_answer"})
    g.add_edge("generate", END)
    g.add_edge("no_answer", END)
    return g.compile()


def run(inbound: InboundEvent, *, k: int = config.TOP_K, trace: dict | None = None, **kw) -> OutboundEvent:
    """pipeline.answer가 부른다. trace를 주면 state 계측(gate·graded·kept·llm_calls·timings)을 채운다."""
    graph = build_graph(**kw)
    t0 = time.monotonic()
    st = graph.invoke({"question": inbound.text, "k": k, "timings": {}})
    if trace is not None:
        trace.update({"gate": st.get("gate"), "vector_top1": st.get("vector_top1"),
                      "hits": [h.ticket_id for h in st.get("hits", [])], "graded": st.get("graded", []),
                      "kept": [h.ticket_id for h in st.get("kept", [])], "llm_calls": st.get("llm_calls", 0),
                      "timings": {**st.get("timings", {}), "total": time.monotonic() - t0}, "status": st["status"]})
    if not st.get("hits"):
        return inbound.reply(NO_MATCH, status="no_match")
    return inbound.reply(st["text"], citations=st.get("citations", []), status=st["status"])
