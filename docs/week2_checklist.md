# 2주차 체크리스트 — "이벤트로 도는 RAG: 웹훅 → 큐 → 워커 → Slack"

Step은 작업 단계이며 달력 날짜와 무관. 진행 날짜는 docs/log/ 참조.
1주차 산출물(ask.py의 검색+답변 로직, tickets.clean.jsonl 318건, eval 기준선 0.885)은 그대로 쓴다. 검색 품질은 2주차에 손대지 않는다.

목표: 금요일에 **Slack에서 질문을 치면 워커가 답을 다는 경로 하나**가 끝까지 돈다. 워커를 죽여도 메시지가 안 사라지고, 같은 메시지를 두 번 보내도 답은 한 번만 간다.

## Step 1 — 이벤트 계약
- [x] `events/contracts.py`: `InboundEvent`(event_id, source, channel, user, text, ts, reply_to) / `OutboundEvent`(event_id, in_reply_to, text, citations, status) — pydantic, 버전 필드 포함
- [x] 직렬화 왕복 테스트 (`tests/test_contracts.py`): dict → model → JSON → model 동일
- [x] 산출물: 계약 2개 + 테스트 + `docs/events.md`에 필드 표와 예시 JSON 1쌍

## Step 2 — FastAPI 웹훅 어댑터
- [ ] `api/webhook.py`: `POST /events` — 본문을 InboundEvent로 검증, 큐에 publish, 202 반환. 검증 실패는 422
- [ ] 큐가 죽어 있으면 503 (publish 실패를 삼키지 않는다)
- [ ] `curl`로 1건 보내 202 확인, 잘못된 본문 422 확인
- [ ] 산출물: 엔드포인트 1개 + 로컬 curl 기록(docs/log)

## Step 3 — RabbitMQ
- [ ] docker-compose에 rabbitmq(management 포함) 추가, `events.inbound` durable 큐, 메시지 persistent
- [ ] 소비자 설정: prefetch 1, 수동 ack (처리 끝나야 ack, 예외면 nack)
- [ ] 확인: 워커 없이 10건 publish → RabbitMQ 재시작 → 10건 그대로 남아 있음
- [ ] 산출물: compose 서비스 + 큐 선언 코드 + 재시작 생존 확인 기록

## Step 4 — aio-pika 워커 (ask.py 이식)
- [ ] `worker/consumer.py`: 메시지 1건 → InboundEvent → `ask.answer()` → OutboundEvent → `events.outbound` publish → ack
- [ ] 임베딩·Qdrant 클라이언트는 워커 시작 시 1회 로드 (메시지마다 로드 금지)
- [ ] 처리 중 예외: nack(requeue=False) → Step 6의 재시도 경로로
- [ ] 확인: 웹훅 1건 → outbound 큐에 답변 1건, [INC-xxxx] 인용 포함
- [ ] 산출물: 워커 1개 + 끝까지 도는 경로 1개 (웹훅 → 큐 → 워커 → outbound)

## Step 5 — 멱등성
- [ ] Redis `SETNX processed:{event_id}` + TTL(24h). 이미 있으면 처리 없이 ack
- [ ] 확인: 같은 event_id 3번 publish → 답변 1건, 로그에 "duplicate skipped" 2건
- [ ] 경계: SETNX 성공 후 처리 중 죽으면? → 키에 상태(processing/done) 두고 processing 상태 TTL 짧게. 결정을 docs/log에 기록
- [ ] 산출물: 멱등 가드 + 중복 3회 테스트 기록

## Step 6 — TTL 재시도 큐 + DLQ
- [ ] `events.retry` 큐(x-message-ttl 30s, dead-letter → `events.inbound`), `events.dlq` (최종)
- [ ] 헤더 `x-retry-count`로 횟수 추적, 3회 초과 시 DLQ
- [ ] 확인: 워커에서 강제 예외 → retry 큐 경유 3회 → DLQ에 1건, 메시지 원문 보존
- [ ] 산출물: 큐 3개 토폴로지 그림(docs/events.md) + DLQ 도달 확인 기록

## Step 7 — graceful shutdown
- [ ] SIGTERM 수신 시: 새 메시지 소비 중단 → 처리 중인 1건 완료·ack → 연결 종료. 타임아웃 30s
- [ ] 확인: 처리 중(LLM 호출 중) SIGTERM → 답변이 outbound에 도착하고 중복 없음. 타임아웃 초과 시 nack → 재시도 경로
- [ ] 산출물: 종료 핸들러 + 처리 중 종료 테스트 기록

## Step 8 — Slack Socket Mode 어댑터
- [ ] `adapters/slack.py`: Socket Mode로 멘션·DM 수신 → InboundEvent → 웹훅(또는 직접 publish). outbound 소비 → 스레드 답글
- [ ] 봇 토큰·앱 토큰은 .env, 채널 허용 목록
- [ ] 확인: Slack에서 "펌프다운이 평소보다 오래 걸리는데" → 스레드에 [INC-xxxx] 인용 답변. 워커 kill 후 재시작해도 답변 도착
- [ ] 산출물: 어댑터 + 실제 Slack 왕복 1건 스크린샷/로그 + README 실행 절차 갱신

## 주차 마무리
- [ ] `docs/events.md` 완성: 계약, 큐 토폴로지, 실패 경로(재시도·DLQ·중복·종료) 표
- [ ] 일지: 결정 칸에 "RabbitMQ vs Kafka", "SETNX vs DB unique", "retry TTL vs 즉시 requeue" 근거
- [ ] 한 줄 후보 1개: 유실·중복·종료 중 하나를 실제로 깨뜨려 본 경험

## 하지 않는 것 (3주차)
- BM25/하이브리드, negative LLM 판정, LangGraph, 카탈로그 전공 검토 마무리, 사람 질의 #human 채우기
