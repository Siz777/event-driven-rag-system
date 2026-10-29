# 2주차 요약 — 이벤트로 도는 RAG: Slack → 큐 → 워커 → Slack

계획 단위: 2주차 / 실제 기간: 2026-10-29

목표였던 "Slack에서 질문을 치면 워커가 답을 다는 경로 하나"가 컨테이너 6개로 끝까지 돈다. 워커를 죽여도 메시지가 안 사라지고(재시작 생존 10→10), 같은 메시지를 두 번 보내도 답은 한 번만 간다(멱등 가드).

## Step별 커밋

| Step | 내용 | 커밋 | 테스트 |
|---|---|---|---|
| 1 | 이벤트 계약 InboundEvent/OutboundEvent (pydantic, version, extra=forbid) | `05f3ccb` | test_contracts 8 |
| 2 | Slack Events 웹훅 + HMAC 서명 검증 (401/400/200 ignored/503/202) | `e499cc1` | test_webhook 13 |
| 3 | RabbitMQ 토폴로지(events·dlx, ai_tasks, retry 5s/25s/125s, dlq, ai_results) + publisher(confirms, persistent) | `b2881ea` | test_mq 6 (integration 3) |
| 4·6·7 | 워커: prefetch 1·수동 ack, x-retry-count → TTL 큐, 3회 초과 DLQ, 독 메시지 DLQ, graceful drain. rag/pipeline 분리 | `d467a3f` | test_pipeline 8, test_worker 10 (integration) |
| 5 | Redis 멱등 가드 SET NX EX 86400, processing/done, stale 300s, fail-open | `601fc91` | test_worker +5 |
| 6(Docker) | Dockerfile.api/worker, compose 전체 경로, CPU torch 고정, exec CMD + init | `d6cda3a` | 수동 |
| 7(Slack) | Socket Mode 어댑터 + ai_results 답글 소비자 + progress chat.update, 계약 reply_to·progress | `d4033c9` | test_slack_adapter 22 |

테스트 84개 = 유닛 71(5.9s) + integration 13(RabbitMQ·Redis 필요, 40s). integration은 compose `worker`를 내리고 돌린다(같은 큐를 소비해 메시지를 가져감).

## 수치

| 항목 | 값 |
|---|---|
| 이미지 | api 267MB · worker 2.26GB(10.2GB → CPU torch 휠 고정) · slack 255MB |
| 재시작 생존 | 워커 없이 10건 publish → `docker compose restart rabbitmq` → 10건(messages_persistent 10) |
| 접수 | 웹훅 202 0.03~0.06s · Socket Mode publish+progress 0.35~0.39s |
| 워커 처리 | 13.4~25.5s (bge-m3 CPU 임베딩 + gpt-5-mini, 인용 5건) |
| 질문 → Slack 답변 | 18.6~21.8s (latency_from_question, 새 질문 기준) |
| graceful stop | SIGTERM → 처리 중 1건 완료 → exit 0, 16~19s. 유실 0, 중복 0 |
| 색인 | 318건, 컨테이너 CPU 5분(최초 1회) |

## graceful drain 로그 (처리 중 `docker compose stop worker`)

```
07:17:37 INFO worker: processing Ev-docker-2 (retry 0) text='펌프다운이 평소보다 오래 걸리는데 뭘 먼저 봐야 해?'
07:17:41 INFO worker: signal SIGTERM received: stop requested
07:17:41 INFO worker: stopping: cancelling consumer (no new messages)
07:17:41 INFO worker: draining 1 in-flight (timeout 120s)…
07:17:58 INFO worker: done Ev-docker-2 → ai_results status=answered citations=5 (21.0s)
07:17:58 INFO worker: draining done
07:17:58 INFO worker: connection closed. processed=2 failed=0 duplicates=0
```
stop 19s, exit 0, OOM false. ai_tasks 0, ai_results 2. 같은 실험을 로컬(Ctrl+C, 21s)·컨테이너(16s·19s) 3회.

## 설계 결정 9개

1. **RabbitMQ, Kafka 아님** — 작업 큐 문제(공정 분배·유실 방지)이고 DLQ·TTL 재시도가 내장. 처리량·재생 요구 없음. 재검토 조건: 초당 수백 건 이상 또는 이벤트 재생 필요.
2. **event_id는 생산자가 정한다(기본값 없음)** — 서버 uuid면 재전송마다 키가 달라져 멱등 가드가 무의미.
3. **OutboundEvent가 channel·reply_to를 들고 다닌다** — 답 보내는 쪽이 inbound를 조회하지 않아야 상태 없이 재시작.
4. **웹훅은 "무시"도 2xx** — Slack은 2xx 아니면 재전송. 큐 장애만 503(재전송 유도).
5. **재시도는 TTL 큐 3단 + x-retry-count, 즉시 requeue 안 씀** — 즉시 requeue는 같은 실패를 초당 수십 번(LLM이면 비용), 횟수도 못 셈.
6. **멱등은 Redis SET NX EX, DB unique 아님, 소비 지점에만** — TTL 있는 키 1개면 충분, DB 없음. 웹훅 앞에서 거르면 publish 실패 재전송과 구분 못 해 질문 유실. Redis 장애는 fail-open(중복 답 < 무응답).
7. **파이프라인은 to_thread** — 동기 LLM 호출이 이벤트 루프를 막으면 AMQP heartbeat 끊겨 연결 사망.
8. **컨테이너는 exec form CMD + init: true, 이미지는 코드만** — shell form이면 SIGTERM이 python에 안 가 답 유실. 가중치·데이터는 볼륨, torch는 CPU 휠 고정(nvidia 3.3GB 차단, constraints + 빌드 시 검증).
9. **Slack은 Socket Mode, progress는 큐 밖에서** — 공개 URL 없이 compose에서 바로. "검색 중…"은 어댑터가 직접 올리고 ts를 Redis에 둬 답을 chat.update. 편집 실패·Redis 불가는 새 답글 폴백(UX이지 정합성 아님). publish 실패는 재전송 없이 유실(웹훅과 다름, 의도적 단순화).

## 깨뜨려 본 것
- 유실: 브로커 재시작(10→10), 워커 처리 중 SIGTERM 3회(답 도착), 브로커 포트 닫힘(웹훅 503).
- 중복: 같은 event_id 2건(답 1, duplicates 1), 실패 후 재시도 사본(키 해제로 중복 아님), stale processing 키(재처리).
- 종료: shell form CMD면 SIGKILL — exec form + tini로 고침. drain 타임아웃 초과 경로는 미실험(3주차).

## 안 한 것 / 3주차로
- retry 3단 155s 전 구간 수동 통과(test_c는 count≥3 → DLQ만), drain 타임아웃 초과 nack 경로.
- latency_from_question은 스레드 root ts 기준 — 스레드 안 질문은 틀림. 질문 ts를 OutboundEvent에 싣는 워밍업으로.
- qdrant-client 1.20 vs 서버 1.12.4 경고.
