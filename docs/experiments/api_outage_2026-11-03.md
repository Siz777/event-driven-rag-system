# 외부 LLM 장애 — 실패 경로 실전 확인 (2026-11-03)

## 무슨 일이 있었나
11-02 23시~11-03 03시, 노이즈 주입 그래프 평가 중 LLM 호출이 연쇄 실패. 482건 중 261건, 재실행 158건 전부 error. 로그에는 `[warn] APIConnectionError` 148회와 `[retry n/2] 1~3s 대기` 1,500회가 섞여 있어 네트워크 장애로 보였다. 직접 한 번 호출해 보니:
```
RateLimitError 429 {'error': {'message': 'You have no credits remaining. Add credits to continue using the API …',
                            'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}
```
**OpenAI 크레딧 소진.** 이날 호출량: graph 평가 964 + batch 298 + 노이즈 ~1,400 + 생성·판정 샘플. `rag.llm.chat`은 429를 전부 레이트리밋으로 보고 retry-after(1~3s)만큼 기다렸다 재시도했다 — 기다려도 소용없는 429를 1,500번 기다렸다. 11-03 11시 재확인 때는 호출이 정상(크레딧 충전됨).

## 고친 것
1. `rag/llm.py`: 429 본문의 `type`/`code`를 WARNING 로그로 찍는다(`429 from gpt-5-mini: type=insufficient_quota code=credit_balance_exhausted (attempt 1/2)`). `insufficient_quota`·`credit_balance_exhausted`·`billing_*`면 재시도 없이 즉시 raise. `rate_limit_exceeded`는 전처럼 retry-after 대기 후 재시도. 테스트 2개(tests/test_llm.py: 즉시 실패·sleep 0회 / 레이트리밋 재시도 3회).
2. `worker/main.py`: 재시도 소진으로 DLQ에 보낼 때 **ai_results에 status=error 답을 같이 publish**. 전에는 DLQ에만 넣고 끝이라 사용자는 "검색 중…"에서 영원히 멈춰 있었다(2주차 계약에 status=error와 Slack 어댑터의 "답변 생성 실패" 문구는 있었는데 워커가 한 번도 내보내지 않았다). test_c 확장: DLQ 1 + ai_results에 error 1(in_reply_to·사유).

## 실패 경로 실전 확인 (외부 LLM 장애 재현: 잘못된 API 키의 호스트 워커 + compose rabbitmq/redis/qdrant/api/slack)
크레딧이 이미 충전돼 429는 재현이 안 돼 401(AuthenticationError)로 같은 경로를 탔다 — 4xx는 `chat`이 즉시 raise하므로 워커 입장에선 동일.
```
11:29:34 curl → api 202 {"status":"accepted","event_id":"Ev-outage-2"}
11:29:34,228 processing Ev-outage-2 (retry 0)
11:29:35,646 WARNING failed Ev-outage-2 (retry 0→1) → ai_tasks.retry.5s:   AuthenticationError: Error code: 401 …
11:29:40,658 processing Ev-outage-2 (retry 1)                                 ← 5s 뒤 복귀
11:29:41,110 WARNING failed Ev-outage-2 (retry 1→2) → ai_tasks.retry.25s
11:30:06,124 processing Ev-outage-2 (retry 2)                                 ← 25s 뒤
11:30:06,564 WARNING failed Ev-outage-2 (retry 2→3) → ai_tasks.retry.125s
11:32:11,586 processing Ev-outage-2 (retry 3)                                 ← 125s 뒤
11:32:12,429 WARNING failed Ev-outage-2 (retry 3→4) → ai_tasks.dlq
11:32:12,445 WARNING gave up Ev-outage-2 after 3 retries → ai_tasks.dlq + error reply to ai_results
slack-1 | 02:32:12,830 ERROR adapters.slack_reply: dropped f1a3c8d3… (in_reply_to Ev-outage-2): slack channel_not_found
```
- 질문 → DLQ까지 **158초**(5 + 25 + 125 + 처리 4회 ~3s). 그동안 사용자는 "검색 중…" 상태.
- DLQ 메시지: x-retry-count 4, x-last-error `AuthenticationError: Error code: 401 …`, 원문 보존. 큐 상태: ai_tasks 0 · retry 0/0/0 · dlq 1 · ai_results 0(slack이 소비).
- Slack 어댑터는 error 답을 받아 `chat.postMessage`를 시도했고, 테스트용 채널(C0123FAB, 픽스처)이라 `channel_not_found` → 영구 오류 규칙대로 로그 + ack(폐기). 실제 채널이었다면 질문 스레드의 "검색 중…" 메시지가 아래 문구로 바뀐다(`format_reply`):
```
답변 생성에 실패했습니다. 잠시 후 다시 질문해 주세요. (사유: AuthenticationError: Error code: 401 - {'error': {'message': 'Incorrect API key )
```
- 멱등 키: 실패마다 release되므로 재시도 사본이 중복으로 안 걸림(duplicates 0). 호스트 워커 종료 후 compose worker 재기동, DLQ는 purge.

## 배운 것
- 429는 둘이다. 로그에 code가 없으면 레이트리밋과 소진을 구분할 수 없고, 소진을 기다리는 재시도는 시간만 태운다.
- "실패 경로"는 큐까지만 보면 반쪽이다 — DLQ에 넣는 것과 사용자에게 알리는 것은 다른 일이고, 둘째를 빼먹었다. 2주차 실패 표에는 "DLQ 보존"만 있었다.
- 통합 테스트는 compose의 worker뿐 아니라 **slack 컨테이너도** 내려야 한다. slack이 ai_results를 가져가 test_c의 error 답이 0으로 보였다.
- 장애 중 compose worker는 2주차 이미지로 돌고 있었다(langgraph 없는 옛 pipeline). 코드가 바뀌면 `docker compose build` — healthy는 이미지 최신 여부를 말해 주지 않는다.
