"""MCP 서버를 stdio로 띄워 도구 1개를 호출하고 결과를 찍는다(클라이언트 측 확인·호출 로그 확보용).

python -m scripts.mcp_call list
python -m scripts.mcp_call search_tickets '{"query": "E-5201 알람 처리 사례", "k": 5}'
python -m scripts.mcp_call ask_incident '{"question": "펌프다운이 평소보다 오래 걸리는데"}'
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def _main(tool: str, args: dict) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, client_info=types.Implementation(name="scripts.mcp_call", version="0.1")) as s:
            init = await s.initialize()
            print(f"server: {init.server_info.name} {init.server_info.version}", file=sys.stderr)
            if tool == "list":
                for t in (await s.list_tools()).tools:
                    print(f"- {t.name}: {t.description}\n  input: {json.dumps(t.input_schema.get('properties', {}), ensure_ascii=False)}")
                return
            t0 = time.monotonic()
            res = await s.call_tool(tool, args, read_timeout_seconds=300)
            print(f"[{tool}] {time.monotonic() - t0:.1f}s isError={res.is_error}", file=sys.stderr)
            if res.structured_content is not None:
                print(json.dumps(res.structured_content, ensure_ascii=False, indent=1))
            else:
                for c in res.content:
                    print(getattr(c, "text", c))


if __name__ == "__main__":
    tool = sys.argv[1] if len(sys.argv) > 1 else "list"
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    asyncio.run(_main(tool, args))
