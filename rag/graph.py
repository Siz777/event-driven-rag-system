"""LangGraph 답변 그래프 (3주차 Step 4): retrieve → grade → (clarify | generate | no_answer) → respond.

grade 2단
 (a) cosine 보조: 벡터 top1 cosine < MIN_COSINE(0.50)이면 LLM 없이 no_answer. positive 최저 0.535(11-02 측정)보다 낮은 안전선이라
     negative 일부만 걸린다. 코드 분기(BM25 순위만, cosine 없음)는 이 단계를 건너뛴다.
 (b) LLM 판정: 상위 k건 각각 "이 티켓이 질문과 같은 설비·증상·원인인가" yes/no (LLM_MODEL_SMALL, 스레드 병렬).
     yes가 MIN_YES(2)건 미만이면 no_answer. 1건만 yes인 경우는 11-02 평가에서 negative 4건(소음·커넥터·냄새처럼 느슨하게 겹치는 티켓
     하나)뿐이었고 positive는 0건 — 5건 중 1건만 yes면 근거가 아니라 우연으로 본다.
clarify(Step 9, 11-03): grade를 통과한 티켓(kept)이 설비 2종 이상에 걸치고 질의에 설비·코드가 없으면 답 대신 설비를 묻는다
  (rag/clarify.py). grade **뒤**에 두는 이유: 검색 결과만 보면 negative 39건 중 25건이 설비 혼재로 되묻기에 걸린다(11-03 측정) —
  "이력 없음"을 되묻기가 가려서는 안 되므로 LLM 판정이 negative를 거른 뒤에만 묻는다. 비용: 되묻은 질의만 grade 2회.
  되묻기 답은 원 질의에 "(설비: X)"를 붙여 다시 들어오므로(worker) 라우팅 필터가 걸리고 조건에서 빠진다 — 세션당 1회.
generate 후검증: 답변에 검색 결과 티켓 인용이 0건이면 no_answer(근거 없는 답은 내보내지 않는다).
no_answer 본문: "검색 상위 k건 중 판정된 티켓 없음" + 가장 가까운 티켓 1건 "참고(유사도 낮음)" + 신규 티켓 등록 안내.
  본문이 "유사 이력 없음"으로 시작하지 않는 이유: Slack 어댑터(format_reply)가 그 문구로 시작하는 본문을 통째로 버리고 자기 문구만 보낸다.
  status=no_match이면 어댑터가 머리말을 붙이므로 본문은 상세만 담는다.

검색·LLM·판정은 주입 가능(테스트는 fake). 예외는 삼키지 않는다 — 워커 재시도 경로가 처리.
state.timings/llm_calls는 평가(scripts/eval_graph.py)용 계측. run()은 llm.track()으로 질의당 토큰·비용·지연을 모아 trace["usage"]와
INFO 로그("llm usage …")에 남긴다 — 워커 로그에서 질의별 비용이 보인다. LLM_TOKEN_BUDGET을 넘기면 TokenBudgetExceeded(워커 재시도 경로).

GRADE_MODE(판정 백엔드): api = per_doc(기본, 티켓별 API 호출) | batch(k건 1호출) | cross_encoder(로컬 cross-encoder, rag/reranker.py —
판정의 외부 전송 0회·LLM 호출 0. 11-04 Step 10: scripts/build_pairs.py → scripts/train_reranker.py로 고른 모델 + 임계값)
| two_stage(cross-encoder 점수가 band [lo, hi] 밖이면 로컬 확정, 안이면 그 티켓만 API 판정 — 외부 전송은 애매한 티켓만).

GRAPH_TOOLS=1(Step 11 Day 2): generate가 OpenAI tool calling으로 rag/tools.aggregate_tickets를 **필요할 때 1회** 부른다 —
"얼마나 자주·평균 복구 시간·가장 최근·어느 설비" 같은 집계 질문은 상위 5건만으로 답할 수 없어서. 도구를 안 부르면 호출 수 그대로(생성 1회),
부르면 생성 2회(도구 결정 + 결과 반영). 집계 결과의 recent_ticket_ids도 인용 대상(DB 행이므로 근거다).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from events import Citation, InboundEvent, OutboundEvent

from . import config
from . import llm
from .clarify import ClarifyDecision, clarify_text, should_clarify
from .retrieve import Hit

log = logging.getLogger("rag.graph")

MIN_COSINE = float(os.getenv("GRADE_MIN_COSINE", "0.50"))
MIN_YES = int(os.getenv("GRADE_MIN_YES", "2"))
GRADE_MODE = os.getenv("GRADE_MODE", "per_doc")      # api|per_doc: 티켓별 호출(병렬) / batch: k건 1호출 / cross_encoder: 로컬 리랭커 / two_stage: 로컬+애매만 API
GRADE_MODES = ("api", "per_doc", "batch", "cross_encoder", "two_stage")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
GRADE_TIMEOUT = float(os.getenv("GRADE_TIMEOUT", "30"))
CLARIFY = os.getenv("GRAPH_CLARIFY", "1") == "1"       # 설비 되묻기 on/off(평가 비교용)
TOOLS = os.getenv("GRAPH_TOOLS", "1") == "1"           # generate의 집계 도구 호출 on/off. 기본 on(11-09 평가: 오호출 0/65, multi_hop 숫자 정확도 0.23 → 0.57, 토큰 +16%)
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

TOOLS_NOTE = """

<tools>
집계 도구 aggregate_tickets가 있다. 발생 빈도·평균/중앙값 복구 시간(다운타임)·최근 발생일·설비별 분포·가장 흔한 조치처럼 [과거 티켓] 몇 건으로는
답할 수 없는 "이력 종합" 질문에만 1회 부른다. 조건은 [과거 티켓]에 적힌 원인 ID(Cxx)·설비 타입·알람 코드로 준다. 단순 유사 사례·조치 순서 질문에는 부르지 않는다.
도구 결과를 쓸 때는 숫자를 그대로 쓰고 "전체 이력 N건 기준"이라고 밝힌다. 결과의 recent_ticket_ids는 [INC-xxxx]로 인용할 수 있다.
</tools>"""

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
GradeCrossFn = Callable[[str, list[Hit]], list[bool]]
ScoresFn = Callable[[str, list[Hit]], list[float]]
ToolChatFn = Callable[[list[dict], list[dict] | None], object]   # (messages, tools) → message(.content, .tool_calls)
ToolExecFn = Callable[[str, str], dict]


class State(TypedDict, total=False):
    question: str
    k: int
    hits: list[Hit]
    vector_top1: float | None
    route: str                 # retrieve 분기(code/type/none…)
    graded: list[bool]
    kept: list[Hit]
    gate: str                  # cosine / llm / pass / empty
    clarify: bool              # 되묻기 허용 여부(세션 2회째는 False)
    candidates: list[str]      # 되묻기 후보 설비(발동 시)
    cross_scores: list[float]  # two_stage: 리랭커 점수
    ambiguous: int             # two_stage: API로 넘긴 티켓 수
    tool_calls: list[dict]     # generate의 도구 호출 기록 [{name, args, result 요약}]
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


def default_tool_chat(messages: list[dict], tools: list[dict] | None):
    from .llm import complete
    return complete(messages, tools=tools, timeout=LLM_TIMEOUT, max_attempts=2)


def default_tool_exec(name: str, arguments: str) -> dict:
    from .tools import run_tool
    return run_tool(name, arguments)


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


def default_scores_cross_encoder(question: str, hits: list[Hit]) -> list[float]:
    from .reranker import pair_text, scores
    return scores(question, [pair_text(h.payload) for h in hits])


def default_band() -> tuple[float, float]:
    from .reranker import band
    return band()


def default_grade_cross_encoder(question: str, hits: list[Hit]) -> list[bool]:
    """로컬 cross-encoder 판정(rag/reranker.py): (question, ticket_text+최종 원인) 쌍 → 관련 확률 ≥ 임계값. LLM 호출 0, 외부 전송 0.
    모델·임계값은 RERANKER_MODEL / RERANKER_THRESHOLD(없으면 artifacts/reranker/best + manifest, 그것도 없으면 허브 제로샷·0.5)."""
    from .reranker import grade_cross
    return grade_cross(question, hits)


def _sim(h: Hit) -> str:
    return f" (유사도 {h.cosine:.2f})" if h.cosine is not None else ""


def build_context(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{h.ticket_id}]{_sim(h)}\n{h.payload['_text']}\n최종 원인: {h.payload['root_cause']}"
                       + (f" (원인 ID {h.payload['cause_id']})" if h.payload.get("cause_id") else "") for h in hits)


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
                grade_batch: GradeBatchFn = default_grade_batch, grade_cross: GradeCrossFn = default_grade_cross_encoder,
                cross_scores: ScoresFn = default_scores_cross_encoder, band: tuple[float, float] | None = None,
                grade_mode: str = GRADE_MODE, min_cosine: float = MIN_COSINE, min_yes: int = MIN_YES, parallel: int = 5,
                clarify: bool = CLARIFY, tools: bool = TOOLS, tool_chat: ToolChatFn = default_tool_chat,
                tool_exec: ToolExecFn = default_tool_exec):
    if grade_mode not in GRADE_MODES:
        raise ValueError(f"unknown GRADE_MODE: {grade_mode} (expected one of {GRADE_MODES})")
    if grade_mode == "api":
        grade_mode = "per_doc"
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
        st_extra: dict = {}
        if grade_mode == "batch":
            graded = [bool(x) for x in grade_batch(st["question"], hits)]
            calls = 1
        elif grade_mode == "cross_encoder":
            graded = [bool(x) for x in grade_cross(st["question"], hits)]     # 로컬 — LLM 호출 0
            calls = 0
        elif grade_mode == "two_stage":
            lo, hi = band or default_band()
            sc = cross_scores(st["question"], hits)
            amb = [i for i, x in enumerate(sc) if lo < x < hi]
            graded = [x >= hi for x in sc]
            if amb:
                ctx = contextvars.copy_context()
                with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(amb)))) as ex:
                    res = list(ex.map(lambda i: ctx.copy().run(lambda: bool(grade(st["question"], hits[i]))), amb))
                for i, r in zip(amb, res):
                    graded[i] = r
            calls = len(amb)
            st_extra = {"cross_scores": [round(x, 4) for x in sc], "ambiguous": len(amb)}
        else:
            ctx = contextvars.copy_context()           # llm.track()의 Usage가 풀 스레드에도 보이게
            with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(hits)))) as ex:
                graded = list(ex.map(lambda h: ctx.copy().run(lambda: bool(grade(st["question"], h))), hits))
            calls = len(hits)
        kept = [h for h, ok in zip(hits, graded) if ok]
        if len(kept) < min(min_yes, len(hits)):          # 검색 결과가 min_yes보다 적으면 전부 yes일 때만 통과
            kept = []
        return {"gate": "llm" if not kept else "pass", "graded": graded, "kept": kept, **st_extra,
                "llm_calls": st["llm_calls"] + calls, "timings": {**st["timings"], "grade": time.monotonic() - t0}}

    def _decision(st: State) -> ClarifyDecision | None:
        if not clarify or not st.get("clarify", True) or not st["kept"]:
            return None
        return should_clarify(st["question"], st["kept"], min_cosine=min_cosine)

    def route(st: State) -> str:
        if not st["kept"]:
            return "no_answer"
        return "clarify" if _decision(st) else "generate"

    def clarify_node(st: State) -> dict:
        d = _decision(st)
        assert d is not None
        return {"status": "clarify", "citations": [], "text": clarify_text(d), "candidates": d.types}

    def generate(st: State) -> dict:
        t0 = time.monotonic()
        kept = st["kept"]
        user = f"[질문]\n{st['question']}\n\n[과거 티켓]\n{build_context(kept)}"
        system = SYSTEM.format(k=st["k"])
        calls, tool_recs, extra_ids = 0, [], []
        if tools:
            from .tools import TOOL_SPEC
            messages = [{"role": "system", "content": system + TOOLS_NOTE}, {"role": "user", "content": user}]
            msg = tool_chat(messages, [TOOL_SPEC])
            calls += 1
            tcs = list(getattr(msg, "tool_calls", None) or [])
            if tcs:
                tc = tcs[0]                               # 1회만 — 두 번째 이후 호출은 무시(비용 상한)
                result = tool_exec(tc.function.name, tc.function.arguments)
                try:
                    args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else dict(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": str(tc.function.arguments)[:200]}
                dm = result.get("downtime_min") or {}
                tool_recs.append({"name": tc.function.name, "args": args, "count": result.get("count"), "error": result.get("error"),
                                  "mean": dm.get("mean"), "median": dm.get("median"), "last_seen": result.get("last_seen"),
                                  "recent": (result.get("recent_ticket_ids") or [None])[0], "ignored_calls": len(tcs) - 1})
                extra_ids = list(result.get("recent_ticket_ids") or [])
                messages.append({"role": "assistant", "content": msg.content or None,
                                 "tool_calls": [{"id": tc.id, "type": "function",
                                                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}]})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})
                msg = tool_chat(messages, None)
                calls += 1
            text = (msg.content or "").strip()
        else:
            text = chat(user, system).strip()
            calls = 1
        by_id = {h.ticket_id: h for h in kept}
        cited: list[Citation] = []
        for tid in _CITE.findall(text):
            if tid in {c.ticket_id for c in cited}:
                continue
            if tid in by_id:
                c = by_id[tid].cosine
                cited.append(Citation(ticket_id=tid, score=round(c, 4) if c is not None else None))
            elif tid in extra_ids:                        # 집계 결과의 최근 티켓 — DB 행이라 근거로 인정
                cited.append(Citation(ticket_id=tid, score=None))
        out = {"llm_calls": st["llm_calls"] + calls, "tool_calls": tool_recs,
               "timings": {**st["timings"], "generate": time.monotonic() - t0}}
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
    g.add_node("clarify", clarify_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", route, {"generate": "generate", "no_answer": "no_answer", "clarify": "clarify"})
    g.add_edge("generate", END)
    g.add_edge("no_answer", END)
    g.add_edge("clarify", END)
    return g.compile()


def run(inbound: InboundEvent, *, k: int = config.TOP_K, trace: dict | None = None, allow_clarify: bool = True, **kw) -> OutboundEvent:
    """pipeline.answer가 부른다. trace를 주면 state 계측(gate·graded·kept·llm_calls·timings·candidates)을 채운다.
    allow_clarify=False는 되묻기 답이 해석되지 않았을 때 원 질의를 그대로 답하는 2회째 호출용(무한 왕복 금지)."""
    graph = build_graph(**kw)
    t0 = time.monotonic()
    with llm.track() as usage:
        st = graph.invoke({"question": inbound.text, "k": k, "timings": {}, "clarify": allow_clarify})
    total = time.monotonic() - t0
    log.info("llm usage %s status=%s total_sec=%.1f text=%r", usage.summary(), st["status"], total, inbound.text[:40])
    if trace is not None:
        trace.update({"gate": st.get("gate"), "vector_top1": st.get("vector_top1"),
                      "hits": [h.ticket_id for h in st.get("hits", [])], "graded": st.get("graded", []),
                      "kept": [h.ticket_id for h in st.get("kept", [])], "llm_calls": st.get("llm_calls", 0),
                      "candidates": st.get("candidates", []), "usage": usage.as_dict(),
                      "cross_scores": st.get("cross_scores"), "ambiguous": st.get("ambiguous"), "tool_calls": st.get("tool_calls", []),
                      "timings": {**st.get("timings", {}), "total": total}, "status": st["status"]})
    if not st.get("hits"):
        return inbound.reply(NO_MATCH, status="no_match")
    return inbound.reply(st["text"], citations=st.get("citations", []), status=st["status"])
