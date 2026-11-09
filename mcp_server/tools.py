"""MCP 도구 본체(전송 계층 없음 — 테스트는 여기를 fake 주입으로 친다). 큐를 거치지 않고 rag를 직접 호출하는 **동기 입구**.

도구 3개
- search_tickets(query, k): hybrid+filter 검색(rag.retrieve.filtered_search). 판정·생성 없음, LLM 호출 0. 적용된 필터(alarm_code·설비)도 돌려준다.
- get_ticket(ticket_id): 티켓 원문 1건(domains/<domain>/data/tickets.clean.jsonl). 생성 메타(_reverse_pred·gen_model·persona·prompt_version)는 뺀다.
- ask_incident(question, allow_clarify): 전체 파이프라인(rag.pipeline.answer = LangGraph retrieve→grade→clarify→generate). 워커와 같은 함수,
  같은 status(answered/no_match/clarify). clarify면 text가 되묻는 질문이고 candidates에 후보 설비 — 호출자(LLM)가 사용자에게 묻고
  설비를 붙여 다시 부르면 된다(워커의 Redis 300s 상태 대신 호출자가 대화를 들고 있다).

호출 로그: 도구마다 1줄(JSON)을 MCP_CALL_LOG(기본 logs/mcp_calls.jsonl)에 남긴다 — 어떤 클라이언트가 어떤 인자로 불렀고 얼마나 걸렸나.
stdout에는 아무것도 쓰지 않는다(stdio 전송 채널). 로그는 logging(stderr) + 파일.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Callable

from events import InboundEvent, OutboundEvent
from rag import config
from rag.retrieve import Hit

log = logging.getLogger("mcp_server.tools")
CALL_LOG = Path(os.getenv("MCP_CALL_LOG", str(config.ROOT / "logs" / "mcp_calls.jsonl")))
INTERNAL_FIELDS = ("_reverse_pred", "gen_model", "persona", "prompt_version", "_text")
HIT_FIELDS = ("equipment_type", "equipment_id", "subsystem", "alarm_code", "date", "downtime_min", "symptom", "root_cause",
              "canonical_resolution")

SearchFn = Callable[..., list[Hit]]
AnswerFn = Callable[..., OutboundEvent]


def _default_search(query: str, k: int, trace: dict) -> list[Hit]:
    from rag.retrieve import filtered_search
    return filtered_search(query, k, trace=trace)


def _default_answer(inbound: InboundEvent, trace: dict, allow_clarify: bool) -> OutboundEvent:
    from rag.pipeline import answer
    return answer(inbound, trace=trace, allow_clarify=allow_clarify)


@lru_cache(maxsize=1)
def _tickets() -> dict[str, dict]:
    from rag.documents import load_tickets
    return {t["ticket_id"]: t for t in load_tickets()}


def _log_call(tool: str, args: dict, summary: dict, t0: float, client: str | None = None) -> None:
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "args": args, "ms": round((time.monotonic() - t0) * 1000),
           "client": client, **summary}
    log.info("tool %s %s -> %s (%d ms)", tool, json.dumps(args, ensure_ascii=False), json.dumps(summary, ensure_ascii=False), rec["ms"])
    try:
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:                                  # 로그 실패가 도구 실패가 되면 안 된다
        log.warning("call log write failed: %s", e)


def hit_view(h: Hit) -> dict:
    p = h.payload
    return {"ticket_id": h.ticket_id, "score": round(float(h.score), 4), "cosine": round(h.cosine, 4) if h.cosine is not None else None,
            **{k: p.get(k) for k in HIT_FIELDS}}


def search_tickets(query: str, k: int = config.TOP_K, *, search: SearchFn = _default_search, client: str | None = None) -> dict:
    t0 = time.monotonic()
    k = max(1, min(int(k), 20))
    trace: dict = {}
    hits = search(query, k, trace)
    out = {"query": query, "k": k, "filters": trace.get("filters", {}), "applied": trace.get("applied", "none"),
           "hits": [hit_view(h) for h in hits]}
    _log_call("search_tickets", {"query": query, "k": k}, {"n": len(hits), "applied": out["applied"],
                                                          "top": [h.ticket_id for h in hits[:3]]}, t0, client)
    return out


def get_ticket(ticket_id: str, *, tickets: dict[str, dict] | None = None, client: str | None = None) -> dict:
    t0 = time.monotonic()
    tid = ticket_id.strip().upper()
    t = (tickets if tickets is not None else _tickets()).get(tid)
    if t is None:
        _log_call("get_ticket", {"ticket_id": ticket_id}, {"found": False}, t0, client)
        return {"error": f"ticket not found: {ticket_id}", "hint": "ID 형식은 INC-2026-0001. search_tickets로 먼저 찾으세요."}
    out = {k: v for k, v in t.items() if k not in INTERNAL_FIELDS}
    _log_call("get_ticket", {"ticket_id": ticket_id}, {"found": True, "cause_id": t.get("cause_id")}, t0, client)
    return out


def ask_incident(question: str, allow_clarify: bool = True, *, answer: AnswerFn = _default_answer, client: str | None = None,
                 user: str | None = None) -> dict:
    t0 = time.monotonic()
    inbound = InboundEvent(event_id=f"mcp-{uuid.uuid4().hex}", source="mcp", channel=client or "mcp", user=user or getpass.getuser(),
                           text=question)
    trace: dict = {}
    out = answer(inbound, trace, allow_clarify)
    res = {"status": out.status, "text": out.text, "citations": [{"ticket_id": c.ticket_id, "score": c.score} for c in out.citations],
           "candidates": trace.get("candidates", []), "gate": trace.get("gate"), "llm_calls": trace.get("llm_calls"),
           "usage": trace.get("usage"), "seconds": round(trace.get("timings", {}).get("total", time.monotonic() - t0), 2),
           "event_id": inbound.event_id}
    _log_call("ask_incident", {"question": question, "allow_clarify": allow_clarify},
              {"status": out.status, "citations": [c.ticket_id for c in out.citations], "llm_calls": res["llm_calls"],
               "tokens": (res["usage"] or {}).get("total_tokens") if res["usage"] else None}, t0, client)
    return res
