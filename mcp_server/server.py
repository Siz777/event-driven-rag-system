"""MCP 서버(stdio). Step 11 — 큐 미경유 **동기 입구**. 도구 4개(search_tickets · get_ticket · aggregate_tickets · ask_incident),
본체는 mcp_server/tools.py, 결정은 docs/events.md §2-c.

실행:  python -m mcp_server.server            (Claude Code: 프로젝트 .mcp.json 에 등록돼 있다 → /mcp 로 확인)
확인:  python -m scripts.mcp_call search_tickets '{"query": "E-5201 알람 처리 사례", "k": 5}'

stdout 보호: stdio 전송은 stdout이 프로토콜 채널이다. rag 쪽 print()(LLM 재시도 경고 등)가 한 줄이라도 섞이면 클라이언트가 JSON 파싱에
실패하므로, 프로토콜용 fd를 따로 복제(dup)해 두고 fd 1은 stderr로 돌린다 — 이후 print()는 전부 stderr.
모델(bge-m3·BM25·Kiwi)은 첫 호출에 올린다(초기화 응답을 막지 않도록 lifespan에서 백그라운드 스레드로 미리 데운다).
"""
from __future__ import annotations

import io
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.stdio import stdio_server

from . import tools

log = logging.getLogger("mcp_server")
INSTRUCTIONS = (
    "반도체 설비 장애 티켓(INC-…) 318건을 검색·조회·질의하는 도구. "
    "search_tickets: 질문/알람 코드/설비명으로 유사 티켓 검색(LLM 없음, 빠름). "
    "get_ticket: 티켓 ID로 원문 1건. "
    "aggregate_tickets: 원인 ID·설비·알람 코드·기간으로 전체 이력 집계(건수·다운타임 평균/중앙값·최근 발생일·조치 상위, LLM 없음). "
    "ask_incident: 전체 파이프라인(검색→관련성 판정→답변 생성, 15~30초, LLM 호출). status=clarify면 text의 질문을 사용자에게 전달하고 "
    "설비명을 붙여 다시 호출."
)


def _warmup() -> None:
    try:
        from rag.retrieve import filtered_search
        filtered_search("워밍업 PECVD-01 E-1101", 1)
        tools._tickets()
        log.info("warmup done")
    except Exception as e:                                # 워밍업 실패는 서버 실패가 아니다 — 첫 호출이 다시 시도한다
        log.warning("warmup failed: %s", e)


@asynccontextmanager
async def lifespan(_server: MCPServer):
    if os.getenv("MCP_WARMUP", "1") == "1":
        threading.Thread(target=_warmup, name="mcp-warmup", daemon=True).start()
    yield {}


server = MCPServer("incident-rag", instructions=INSTRUCTIONS, version="0.1", lifespan=lifespan)


def _client(ctx: Context | None) -> str | None:
    try:
        params = ctx.session.client_params if ctx is not None else None
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        return f"{info.name}/{info.version}" if info else None
    except Exception:
        return None


@server.tool(description="과거 장애 티켓 검색(하이브리드 + 알람 코드·설비 필터). LLM 호출 없음. k 기본 5, 최대 20.")
def search_tickets(query: str, k: int = 5, ctx: Context | None = None) -> dict:
    return tools.search_tickets(query, k, client=_client(ctx))


@server.tool(description="티켓 ID(INC-2026-0001 형식)로 원문 1건 조회: 설비·알람·증상·조치 기록·조치 결과·최종 원인·다운타임(분)·날짜.")
def get_ticket(ticket_id: str, ctx: Context | None = None) -> dict:
    return tools.get_ticket(ticket_id, client=_client(ctx))


@server.tool(description="과거 티켓 전체 집계: 건수, 다운타임(분) 평균·중앙값·최소·최대, 최초·최근 발생일, 가장 흔한 조치, 설비 ID별·원인별 건수, "
                         "최근 티켓 ID. 조건(AND, 하나 이상): cause_id(C01) · equipment_type(PECVD) · equipment_id(PECVD-03) · alarm_code(E-1101) · since(YYYY-MM-DD).")
def aggregate_tickets(cause_id: str | None = None, equipment_type: str | None = None, equipment_id: str | None = None,
                      alarm_code: str | None = None, since: str | None = None, ctx: Context | None = None) -> dict:
    return tools.aggregate(cause_id, equipment_type, equipment_id, alarm_code, since, client=_client(ctx))


@server.tool(description="장애 질문에 근거 티켓과 함께 답변(검색→판정→생성, 15~30초). status: answered | no_match | clarify(설비 되묻기, "
                         "text가 질문·candidates가 후보 설비 — allow_clarify=false면 되묻지 않고 바로 답).")
def ask_incident(question: str, allow_clarify: bool = True, ctx: Context | None = None) -> dict:
    return tools.ask_incident(question, allow_clarify, client=_client(ctx))


async def _run_stdio() -> None:
    proto_fd = os.dup(1)
    os.dup2(2, 1)                                         # 이후 print()·stdout 로그는 stderr로
    proto_out = anyio.wrap_file(io.TextIOWrapper(os.fdopen(proto_fd, "wb", buffering=0), encoding="utf-8", write_through=True))
    stdin = anyio.wrap_file(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
    async with stdio_server(stdin=stdin, stdout=proto_out) as (r, w):
        low = server._lowlevel_server
        await low.run(r, w, low.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    anyio.run(_run_stdio)


if __name__ == "__main__":
    main()
