"""mcp_server — 도구 본체(fake 주입)와 stdio 왕복(도구 목록)."""
import asyncio
import json
import os
import sys

import pytest

from events import InboundEvent, OutboundEvent
from mcp_server import tools
from rag.retrieve import Hit


def hit(tid, score, cosine=None, **payload):
    p = {"ticket_id": tid, "equipment_type": "PECVD", "equipment_id": "PECVD-03", "alarm_code": "E-1101", "symptom": "펌프다운 지연",
         "root_cause": "O-ring 마모", "canonical_resolution": "O-ring 교체", "downtime_min": 180, "date": "2026-02-27", "subsystem": "진공",
         "_text": "본문", "_reverse_pred": "C02", **payload}
    return Hit(tid, score, p, cosine=cosine)


@pytest.fixture(autouse=True)
def call_log(tmp_path, monkeypatch):
    path = tmp_path / "calls.jsonl"
    monkeypatch.setattr(tools, "CALL_LOG", path)
    return path


def test_search_tickets_view_filters_and_log(call_log):
    def fake(query, k, trace):
        trace.update({"filters": {"alarm_codes": ["E-1101"], "equipment_types": []}, "applied": "code"})
        return [hit("INC-2026-0001", 2.3), hit("INC-2026-0002", 0.03, cosine=0.71)]
    out = tools.search_tickets("E-1101 사례", 50, search=fake, client="t/0")
    assert out["k"] == 20 and out["applied"] == "code" and out["filters"]["alarm_codes"] == ["E-1101"]
    assert [h["ticket_id"] for h in out["hits"]] == ["INC-2026-0001", "INC-2026-0002"]
    assert out["hits"][0]["cosine"] is None and out["hits"][1]["cosine"] == 0.71
    assert "_text" not in out["hits"][0] and "_reverse_pred" not in out["hits"][0]      # 내부 필드는 안 나간다
    rec = json.loads(call_log.read_text().splitlines()[-1])
    assert rec["tool"] == "search_tickets" and rec["n"] == 2 and rec["client"] == "t/0" and rec["args"]["k"] == 20


def test_get_ticket_strips_internal_fields_and_not_found():
    tickets = {"INC-2026-0001": {"ticket_id": "INC-2026-0001", "cause_id": "C01", "symptom": "x", "_reverse_pred": "C02",
                                 "gen_model": "gpt", "persona": "p", "prompt_version": "v1", "downtime_min": 60}}
    out = tools.get_ticket(" inc-2026-0001 ", tickets=tickets)
    assert out == {"ticket_id": "INC-2026-0001", "cause_id": "C01", "symptom": "x", "downtime_min": 60}
    miss = tools.get_ticket("INC-9999-0000", tickets=tickets)
    assert "error" in miss and "hint" in miss


def test_ask_incident_maps_outbound_and_trace():
    seen = {}
    def fake(inbound: InboundEvent, trace: dict, allow_clarify: bool):
        seen.update(source=inbound.source, allow=allow_clarify, text=inbound.text)
        trace.update({"candidates": ["PECVD", "PVD"], "gate": "pass", "llm_calls": 2, "timings": {"total": 1.5},
                      "usage": {"total_tokens": 1200, "cost_usd": 0.001}})
        return inbound.reply("어느 설비인가요?", status="clarify")
    out = tools.ask_incident("진공이 안 잡혀요", allow_clarify=True, answer=fake, client="t/0")
    assert seen == {"source": "mcp", "allow": True, "text": "진공이 안 잡혀요"}
    assert out["status"] == "clarify" and out["candidates"] == ["PECVD", "PVD"] and out["citations"] == []
    assert out["llm_calls"] == 2 and out["seconds"] == 1.5 and out["usage"]["total_tokens"] == 1200
    assert out["event_id"].startswith("mcp-")


def test_stdio_round_trip_lists_three_tools():
    """서버를 실제로 띄워(stdio) initialize → tools/list. 모델은 올리지 않는다(MCP_WARMUP=0)."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                       env={**os.environ, "MCP_WARMUP": "0"})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                names = sorted(t.name for t in (await s.list_tools()).tools)
                return init.server_info.name, names
    name, names = asyncio.run(go())
    assert name == "incident-rag" and names == ["ask_incident", "get_ticket", "search_tickets"]
