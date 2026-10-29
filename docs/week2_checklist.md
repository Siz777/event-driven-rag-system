# 2주차 체크리스트 — "이벤트로 도는 RAG: 웹훅 → 큐 → 워커 → Slack"

Step은 작업 단계이며 달력 날짜와 무관. 진행 날짜는 docs/log/ 참조.
1주차 산출물(ask.py의 검색+답변 로직, tickets.clean.jsonl 318건, eval 기준선 0.885)은 그대로 쓴다. 검색 품질은 2주차에 손대지 않는다.

목표: 금요일에 **Slack에서 질문을 치면 워커가 답을 다는 경로 하나**가 끝까지 돈다. 워커를 죽여도 메시지가 안 사라지고, 같은 메시지를 두 번 보내도 답은 한 번만 간다.

## Step 1 — 이벤트 계약
- [x] `events/contracts.py`: `InboundEvent`(event_id, source, channel, user, text, ts, reply_to) / `OutboundEvent`(event_id, in_reply_to, text, citations, status) — pydantic, 버전 필드 포함
- [x] 직렬화 왕복 테스트 (`tests/test_contracts.py`): dict → model → JSON → model 동일
- [x] 산출물: 계약 2개 + 테스트 + `docs/events.md`에 필드 표와 예시 JSON 1쌍

## Step 2 — FastAPI 웹훅 어댑터
- [x] `api/webhook.py`: `POST /events` — Slack 서명 검증(HMAC-SHA256, 5분 창) → url_verification challenge → event_callback을 InboundEvent로 변환, 202 반환. 검증 실패는 200 ignored+로그(Slack 재전송 방지), 서명 실패 401
- [x] 큐가 죽어 있으면 503 (publish 실패를 삼키지 않는다) — publish 주입, 예외 → 503. 지금은 로그만(Step 3에서 RabbitMQ publish로 교체)
- [x] `curl`로 1건 보내 202 확인, 잘못된 본문(멘션만) 200 ignored 확인 (서명 불일치·오래된 타임스탬프 401 포함)
- [x] 산출물: 엔드포인트 1개 + 로컬 curl 기록(docs/log 10-29) + tests/fixtures/slack_app_mention.json + tests/test_webhook.py

## Step 3 — RabbitMQ
- [x] docker-compose에 rabbitmq:3.13-management 추가(5672/15672). `mq/topology.py`: exchange events(direct) · `ai_tasks`(durable, DLX=events.dlx) · `ai_tasks.dlq` · 재시도 3단 `ai_tasks.retry.5s/25s/125s`(TTL, DLX→events 복귀) · `ai_results`. `declare()` 멱등
- [x] `mq/publisher.py`: connect_robust + publisher confirms + PERSISTENT, 본문 InboundEvent.to_json(), 헤더 x-retry-count·event_id. 웹훅 publish 주입을 실제 Publisher로(브로커 불가 → 503, 테스트로 확인)
- [x] 소비자 설정: prefetch 1, 수동 ack (처리 끝나야 ack) — Step 4 워커에서
- [x] 통합 테스트(`pytest -m integration`, 브로커 없으면 skip): publish → 관리 API 큐 깊이 1, 선언 2회 멱등, 웹훅 end-to-end — 3 passed
- [x] 확인: 워커 없이 10건 publish → RabbitMQ 재시작 → 10건 그대로 남아 있음 (10 → 10, messages_persistent 10)
- [x] 수동 확인: 서명된 curl 1회 → 관리 UI ai_tasks 메시지 1개 → 스크린샷 docs/experiments/rabbitmq_queues_2026-10-29.png
- [x] 산출물: compose 서비스 + 큐 선언 코드 + 재시작 생존 확인 기록(docs/log 10-29)

## Step 4 — aio-pika 워커 (ask.py 이식)
- [x] `rag/pipeline.py`: `answer(InboundEvent) -> OutboundEvent` 순수 함수(검색·LLM 주입), no_match 규칙, LLM 타임아웃 90s. ask.py는 호출만
- [x] `worker/main.py`: 메시지 1건 → InboundEvent → `pipeline.answer` → OutboundEvent → `ai_results` publish → ack. prefetch 1, 수동 ack
- [x] 임베딩·Qdrant 클라이언트는 워커 시작 시 1회 로드(warmup 7.8s) (메시지마다 로드 금지)
- [x] 처리 중 예외: x-retry-count+1 → retry 티어 publish → ack (3회 초과 dlq, 독 메시지 즉시 dlq). nack(requeue=True) 안 씀
- [x] graceful shutdown: SIGTERM/SIGINT → consumer cancel → 처리 중 1건 완료 대기(120s) → 종료. 로그 "draining 1 in-flight… done"
- [x] 테스트: pipeline 유닛 8개(fake) + 워커 integration 5개(성공·retry.5s 복귀·dlq·독 메시지·graceful stop)
- [x] 확인: 웹훅 1건 → ai_results에 답변 1건, [INC-xxxx] 인용 포함 (실제 LLM, 17.8s, 인용 5건)
- [x] 확인: LLM 호출 중 Ctrl+C → draining → 21.8s 뒤 답 도착·ack, ai_tasks 0
- [x] 산출물: 워커 1개 + 끝까지 도는 경로 1개 (웹훅 → ai_tasks → 워커 → ai_results)

## Step 5 — 멱등성
- [x] docker-compose에 redis:7-alpine(appendonly), `.env.example` REDIS_URL. Redis `SET idem:{event_id} NX EX 86400`. 이미 있으면 "duplicate, ack only" — 파이프라인·ai_results 없이 ack. 키는 성공 뒤에도 24h 유지
- [x] Redis 연결 실패 → 가드 없이 처리 + WARNING(멱등 가드 비활성). 웹훅 쪽 가드 없음(이유 events.md 4절)
- [x] 확인(integration): 같은 event_id 2건 publish → 답변 1건, 로그 "duplicate, ack only" 1건 / Redis 내려간 상태 → 처리됨 + WARNING
- [x] 경계: 처리 중 죽으면? → 값 processing:{epoch} / done. processing이 300s 넘게 오래되면 재처리, 최근이면 중복. 실패(재시도행)면 키 삭제. 결정 docs/events.md 4절·일지
- [x] 산출물: 멱등 가드(IdempotencyGuard) + 테스트 5개(중복·Redis 다운·실패 시 키 해제·stale 재처리·최근 processing 중복)

## Step 6 — TTL 재시도 큐 + DLQ
- [x] 재시도 큐 3단 `ai_tasks.retry.5s/25s/125s`(x-message-ttl, DLX → events/ai_tasks 복귀), `ai_tasks.dlq` (Step 3에서 선언)
- [x] 헤더 `x-retry-count`로 횟수 추적, 3회 초과 시 DLQ (Step 4 워커, test_c)
- [ ] 확인: 워커에서 강제 예외 → retry 큐 경유 3회 → DLQ에 1건, 메시지 원문 보존
- [ ] 산출물: 큐 3개 토폴로지 그림(docs/events.md) + DLQ 도달 확인 기록

## Step 7 — graceful shutdown
- [x] SIGTERM 수신 시: 새 메시지 소비 중단 → 처리 중인 1건 완료·ack → 연결 종료. 타임아웃 120s(WORKER_DRAIN_TIMEOUT) (Step 4에서 구현)
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
