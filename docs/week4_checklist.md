# 4주차 체크리스트 — Step 11 MCP + Tool Calling (11-09 ~)

## Day 1 — MCP 서버 (동기 입구)
- [x] mcp_server/server.py(stdio, MCP SDK 2.3) + tools.py: search_tickets · get_ticket · ask_incident, 큐 미경유 직접 호출. stdout 보호(fd 1 → stderr)
- [x] Claude Code 클라이언트(.mcp.json, `claude -p --mcp-config`)로 E-5201 검색 호출 로그 확보 — client claude-code/2.1.319, 도구 10 ms. docs/experiments/mcp_call_sample.md
- [x] docs/events.md §2-c "동기 입구 vs 비동기 입구" 결정표
- [x] 테스트 4(도구 fake 3 + stdio 왕복)

## Day 2 — 집계 도구 + tool calling
- [x] rag/tools.py aggregate_tickets(cause_id | equipment_type | equipment_id | alarm_code, since) → count · 다운타임 mean/median/min/max · first/last_seen · top_resolution · by_equipment_id · by_cause · recent_ticket_ids
- [x] rag/llm.complete(tools=…) + generate 노드 tool calling(필요 시 1회, 두 번째 호출 무시), 컨텍스트에 원인 ID, 집계 결과의 최근 티켓도 인용 인정. GRAPH_TOOLS 플래그
- [x] 집계형 평가셋 scripts/build_aggregate_evalset.py → eval_aggregate.jsonl 95행(multi_hop 30 · 조치·시간 23 · 과거 이력 12 · exact 대조 30), 정답 숫자 = cause_id 기준 전체 집계
- [x] scripts/eval_tools.py off/on → multi_hop any_hit 0.233 → 0.567, 호출 20/30, 충실도 0.85, 대조군 오호출 0/65, 토큰 +16%(전체) — README 결과 표
- [x] README 아키텍처(MCP 입구·tool 노드), 결과 표, 일지, GRAPH_TOOLS 기본 on, 커밋·푸시

## MLOps-lite
- [x] scripts/eval_gate.py: 기록값/실측 두 모드, 기준치 = 측정 − 0.02, reranker manifest 검증, exit 1
- [x] .github/workflows/ci.yml: 유닛 테스트(통합 제외) + eval_gate --mode recorded. CPU torch 고정(constraints-worker.txt)
- [x] README "평가 게이트" 절
- [ ] CI 첫 실행 결과 확인(push 후 Actions 탭 — 의존성 설치 ~5분 예상)

## 하지 않는 것
- MCP 원격 전송(streamable-http)·인증, 도구 2회 이상 연쇄, 티켓 쓰기 도구

## Step 12 — 임베딩 모델 비교 (11-20, 검색 단독)
- [x] rag/embed.py: EMBED_MODEL별 질의 프롬프트 분기(Qwen3 instruction은 질의에만), EMBED_DEVICE. 기본 bge-m3 동작 불변(유닛 통과)
- [x] Qwen3-Embedding-0.6B · KURE-v1 색인(모델별 컬렉션, 기존 bge-m3 컬렉션 불변), 색인 시간·메모리 기록
- [x] scripts/eval_embed.py: 124행 · negative 39 · 코드 3종 · 170행 × vector / hybrid+filter. 결과 = hybrid+filter R@5 0.750 / 0.750 / 0.734, 차이 없음(±0.03)
- [x] docs/experiments/step12_embedding.md(표 + 실패 집합 비교 + 결론), 일지 11-20
- [x] eval_gate 기록 모드가 기준 모델 행만 읽도록 고정(비교 실험 행이 최신이어도 게이트 불변)
- [ ] README 반영 여부 — 사용자 결정
