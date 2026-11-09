# MCP 호출 로그 샘플 (Step 11 Day 1, 2026-11-09)

서버 쪽 `logs/mcp_calls.jsonl` (gitignore) 중 Claude Code 클라이언트가 부른 줄과, 그 앞의 스크립트 클라이언트 호출. 클라이언트 식별은 MCP initialize의 clientInfo.

```jsonl
{"at": "2026-11-09T10:42:05", "tool": "search_tickets", "args": {"query": "E-5201 알람 처리 사례", "k": 5}, "ms": 5753, "client": null, "n": 5, "applied": "code", "top": ["INC-2026-0307", "INC-2026-0259", "INC-2026-0278"]}
{"at": "2026-11-09T10:42:24", "tool": "get_ticket", "args": {"ticket_id": "INC-2026-0307"}, "ms": 5, "client": null, "found": true, "cause_id": "C39"}
{"at": "2026-11-09T10:42:58", "tool": "search_tickets", "args": {"query": "E-5201 알람 처리 사례", "k": 5}, "ms": 10, "client": "claude-code/2.1.319", "n": 5, "applied": "code", "top": ["INC-2026-0307", "INC-2026-0259", "INC-2026-0278"]}
```

Claude Code(`claude -p … --mcp-config .mcp.json --strict-mcp-config --allowedTools mcp__incident-rag__search_tickets`) 결과: 3턴, 15.0초(모델 응답 포함; 도구 자체는 10 ms), 호스트 쪽 비용 $0.46(Claude 토큰 — 우리 LLM 호출 0).

```
search_tickets 1회 호출 결과. 필터 `alarm_codes=[E-5201]` 적용(applied=code), 5건 모두 CMP 설비.

| ticket_id | equipment_id | root_cause |
|---|---|---|
| INC-2026-0307 | CMP-07 | 종점 검출 센서 신호 저하 |
| INC-2026-0259 | CMP-06 | 슬러리 공급 라인 응집물로 인한 막힘 |
| INC-2026-0278 | CMP-03 | 다이아몬드 컨디셔너 디스크 마모 |
| INC-2026-0306 | CMP-04 | 종점 검출 센서 신호 저하 |
| INC-2026-0262 | CMP-02 | 슬러리 공급 라인 응집물로 인한 막힘 |

root_cause 3종: 종점검출 센서 2건, 슬러리 라인 막힘 2건, 컨디셔너 마모 1건. 같은 알람 코드에 서브시스템 다름 — 설비명 없이는 원인 특정 어려움.
```
