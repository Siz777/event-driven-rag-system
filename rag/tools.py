"""LLM 도구(Step 11 Day 2): 티켓 집계. generate 노드가 필요할 때 1회 부른다(OpenAI tool calling). MCP 서버도 같은 함수를 노출할 수 있다.

aggregate_tickets(cause_id | equipment_type | alarm_code | equipment_id, since) — 조건은 AND, 하나 이상 필수.
반환: 건수, 다운타임(분) 평균·중앙값·최소·최대, 최초·최근 발생일, 조치(canonical_resolution) 상위, 설비 ID 상위, 원인별 상위(원인을 안 줬을 때),
      최근 티켓 ID 3개(인용용 — 답에 [INC-…]로 넣을 수 있다).
왜 도구인가: 생성 컨텍스트는 판정 통과 상위 5건뿐이라 "평균 복구 시간·발생 빈도·가장 최근 사례" 같은 질문에는 숫자를 지어내거나
"상위 5건 기준"으로 얼버무릴 수밖에 없다. 전체 318건을 읽는 건 토큰 낭비이므로 집계만 코드로 하고 결과를 컨텍스트에 붙인다.
데이터는 domains/<domain>/data/tickets.clean.jsonl(로컬) — 외부로 나가는 건 집계 결과뿐이다.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from functools import lru_cache

from .documents import load_tickets

TOOL_NAME = "aggregate_tickets"
TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": ("과거 장애 티켓 전체(수백 건)에서 조건에 맞는 티켓을 집계한다: 건수, 다운타임(분) 평균·중앙값, 최초·최근 발생일, "
                        "가장 흔한 조치, 설비 ID별 건수, 최근 티켓 ID. '얼마나 자주', '평균 복구 시간/MTTR', '가장 최근', '어느 설비에서 많이', "
                        "'이력 종합' 같은 질문에만 쓴다. 조건은 [과거 티켓]에 보이는 값(원인 ID Cxx, 설비 타입, 알람 코드)으로 준다."),
        "parameters": {
            "type": "object",
            "properties": {
                "cause_id": {"type": "string", "description": "원인 ID (예: C01). [과거 티켓]의 '원인 ID'에서 가져온다"},
                "equipment_type": {"type": "string", "description": "설비 타입 (PECVD, ETCH, PVD, PHOTO, CMP …)"},
                "equipment_id": {"type": "string", "description": "설비 ID (예: PECVD-03)"},
                "alarm_code": {"type": "string", "description": "알람 코드 (예: E-1101)"},
                "since": {"type": "string", "description": "이 날짜(YYYY-MM-DD) 이후만. 생략하면 전체 기간"},
            },
            "additionalProperties": False,
        },
    },
}


@lru_cache(maxsize=1)
def _tickets() -> list[dict]:
    return load_tickets()


def aggregate_tickets(cause_id: str | None = None, equipment_type: str | None = None, equipment_id: str | None = None,
                      alarm_code: str | None = None, since: str | None = None, *, tickets: list[dict] | None = None,
                      top: int = 3) -> dict:
    flt = {k: v.strip().upper() for k, v in (("cause_id", cause_id), ("equipment_type", equipment_type),
                                             ("equipment_id", equipment_id), ("alarm_code", alarm_code)) if v}
    if not flt:
        return {"error": "조건이 없습니다. cause_id / equipment_type / equipment_id / alarm_code 중 하나 이상"}
    rows = [t for t in (tickets if tickets is not None else _tickets())
            if all(str(t.get(k, "")).upper() == v for k, v in flt.items()) and (not since or t.get("date", "") >= since)]
    if not rows:
        return {"filter": {**flt, **({"since": since} if since else {})}, "count": 0}
    dt = [int(t["downtime_min"]) for t in rows if t.get("downtime_min") is not None]
    rows_by_date = sorted(rows, key=lambda t: t.get("date", ""))
    out = {"filter": {**flt, **({"since": since} if since else {})}, "count": len(rows),
           "downtime_min": {"mean": round(statistics.mean(dt), 1), "median": statistics.median(dt), "min": min(dt), "max": max(dt)} if dt else None,
           "first_seen": rows_by_date[0].get("date"), "last_seen": rows_by_date[-1].get("date"),
           "top_resolution": [{"resolution": r, "count": c} for r, c in Counter(t.get("canonical_resolution") for t in rows).most_common(top)],
           "by_equipment_id": [{"equipment_id": e, "count": c} for e, c in Counter(t.get("equipment_id") for t in rows).most_common(top)],
           "recent_ticket_ids": [t["ticket_id"] for t in rows_by_date[-top:]][::-1]}
    if "cause_id" not in flt:
        out["by_cause"] = [{"cause_id": cid, "root_cause": next(t["root_cause"] for t in rows if t["cause_id"] == cid), "count": c}
                           for cid, c in Counter(t["cause_id"] for t in rows).most_common(top)]
    return out


def run_tool(name: str, arguments: str | dict) -> dict:
    """LLM의 tool call 1건 실행. 모르는 도구·깨진 인자는 error dict로(예외를 모델에게 돌려줘야 다음 턴이 이어진다)."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except json.JSONDecodeError as e:
        return {"error": f"arguments is not JSON: {e}"}
    if name != TOOL_NAME:
        return {"error": f"unknown tool: {name}"}
    allowed = set(TOOL_SPEC["function"]["parameters"]["properties"])
    return aggregate_tickets(**{k: v for k, v in args.items() if k in allowed})
