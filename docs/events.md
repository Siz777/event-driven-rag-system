# 이벤트 계약 · 큐 토폴로지 · 실패 경로

2주차 산출물. 어댑터(Slack/HTTP) ↔ RabbitMQ ↔ 워커 사이를 오가는 데이터 형식과 큐 구조를 한 곳에 둔다.
코드: `events/contracts.py`, 테스트: `tests/test_contracts.py` (`python -m pytest`).

## 1. 계약 (version 1)

공통 규칙
- `version`은 `Literal[1]`. 다른 값은 검증 실패. 형식을 바꾸면 숫자를 올리고 소비자가 명시적으로 분기한다.
- 모르는 필드는 거부(`extra="forbid"`). 오타가 조용히 버려지는 것보다 422가 낫다.
- 모델은 불변(`frozen=True`). 워커는 inbound를 고치지 않고 `reply()`로 새 outbound를 만든다.
- `ts`는 timezone-aware만 허용. naive datetime은 어댑터마다 기준이 달라 순서·TTL 계산을 망친다.
- 직렬화: `to_json()` / `from_json()` (pydantic `model_dump_json` / `model_validate_json`). JSON 안에서 `ts`는 ISO 8601 오프셋 포함.

### InboundEvent — 사용자 질문 1건 (`events.inbound`)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| version | 1 | 기본 1 | 스키마 버전 |
| event_id | str (1~128) | ✔ | **생산자가 정하는 멱등 키.** 기본값 없음. 같은 질문의 재전송은 같은 값을 써야 Step 5 멱등 가드가 작동. Slack은 이벤트 ID, HTTP 클라이언트는 자기 uuid |
| source | str (≥1) | ✔ | 어댑터 이름(`slack`/`http`/`cli` …). 등록된 어댑터인지는 계약이 아니라 라우터(Step 2)가 검증 |
| channel | str | ✔ | **어댑터가 해석하는 불투명 문자열.** Slack은 채널 ID, HTTP는 호출자 식별자, CLI는 `terminal`. 코어는 내용을 보지 않고 그대로 outbound로 복사 |
| user | str | ✔ | 질문자 ID |
| text | str (1~2000) | ✔ | 질문 원문. 앞뒤 공백 제거, 공백만이면 거부 |
| ts | datetime (tz-aware) | 기본 now(UTC) | 질문 발생 시각 |
| reply_to | str \| null | 기본 null | **어댑터가 해석하는 불투명 문자열.** Slack은 `thread_ts`. null이면 질문 자체가 새 스레드 |

### OutboundEvent — 질문 1건에 대한 답 1건 (`events.outbound`)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| version | 1 | 기본 1 | 스키마 버전 |
| event_id | str | 기본 uuid4 | 답 이벤트 자체의 ID |
| in_reply_to | str | ✔ | 답하는 `InboundEvent.event_id` |
| channel | str | ✔ | inbound에서 복사(불투명 문자열). 답 보내는 쪽이 inbound를 다시 조회하지 않게 |
| reply_to | str \| null | 기본 null | 답을 달 스레드. `inbound.reply_to` 있으면 그 값, 없으면 `inbound.event_id` |
| text | str (≤4000) | ✔ | 답변 본문. `status=answered`면 비어 있을 수 없음 |
| citations | list[Citation] | 기본 [] | 근거 티켓. `Citation = {ticket_id: "INC-…", score: float\|null (−1~1)}` |
| status | `answered` \| `no_match` \| `error` | ✔ | answered: 근거 있는 답 / no_match: "유사 이력 없음" / error: 처리 실패. `progress`(처리 중 알림)는 Step 7 graceful shutdown과 함께 추가 예정 |
| error | str \| null | status=error면 ✔ | 실패 사유. status≠error면 있으면 안 됨 |
| ts | datetime (tz-aware) | 기본 now(UTC) | 답 생성 시각 |

체크리스트 초안(event_id, in_reply_to, text, citations, status)에 `channel`·`reply_to`·`error`·`ts`를 더했다.
이유: outbound 소비자(Slack 어댑터)가 상태 없이 "어디에 달지"를 알아야 재시작·중복 소비가 단순해진다. `error`는 status=error의 사유를 본문과 섞지 않기 위해.

### 예시 JSON 1쌍

InboundEvent
```json
{
  "version": 1,
  "event_id": "Ev08ABC123",
  "source": "slack",
  "channel": "C0123FAB",
  "user": "U0456ENG",
  "text": "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?",
  "ts": "2026-10-29T09:12:33+09:00",
  "reply_to": null
}
```

OutboundEvent (`inbound.reply(...)`로 생성)
```json
{
  "version": 1,
  "event_id": "9f1c2e7d4b5a4c0e8d6f1a2b3c4d5e6f",
  "in_reply_to": "Ev08ABC123",
  "channel": "C0123FAB",
  "reply_to": "Ev08ABC123",
  "text": "(1) 유사 사례: 펌프다운 지연 2건 [INC-2026-0012][INC-2026-0015] … (3) 확인 순서: 1) 포라인 밸브 …",
  "citations": [
    {"ticket_id": "INC-2026-0012", "score": 0.71},
    {"ticket_id": "INC-2026-0015", "score": 0.66}
  ],
  "status": "answered",
  "error": null,
  "ts": "2026-10-29T09:12:40+09:00"
}
```

## 2. 큐 토폴로지
(Step 3·6에서 채움: `events.inbound` / `events.retry` / `events.dlq` / `events.outbound`)

## 3. 실패 경로
(주차 마무리에서 채움: 재시도·DLQ·중복·종료 표)
