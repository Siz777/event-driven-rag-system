"""이벤트 계약 (pydantic v2).

- InboundEvent : 사용자 질문 1건. 어댑터가 만들고 `events.inbound` 큐로 들어간다.
- OutboundEvent: 질문 1건에 대한 답 1건. 워커가 만들고 `events.outbound` 큐로 나간다.

원칙
- event_id는 생산자가 정한다(기본값 없음). 같은 질문의 재전송은 같은 event_id를 써야 멱등 처리(Step 5)가 된다.
  Slack은 event_id를 주고, HTTP 클라이언트는 자기가 uuid를 만든다.
- ts는 timezone-aware만 허용. naive datetime은 어댑터마다 기준이 달라 순서·TTL 계산을 망친다.
- 모르는 필드는 거부(extra="forbid"). 오타가 조용히 버려지는 것보다 422가 낫다.
- version은 Literal로 고정. 형식이 바뀌면 숫자를 올리고 소비자가 명시적으로 분기한다.
- source는 비어 있지 않은 문자열. 어댑터 목록은 계약이 아니라 라우터(Step 2)가 안다.
- reply_to는 Slack thread_ts(메시지 ts). 어댑터가 질문 메시지 ts를 넣으므로 답은 항상 질문 스레드에 달린다.
- OutboundEvent는 channel/reply_to를 inbound에서 복사해 들고 다닌다.
  답을 보내는 쪽(Slack 어댑터)이 inbound를 다시 조회하지 않아도 되게 — 소비자는 상태가 없어야 재시작이 쉽다.
- status=clarify(3주차 Step 9)는 값 추가만이라 version 1 그대로. 모르는 소비자는 Literal 검증에서 거부한다 — 어댑터 갱신이 먼저.
- asked_at은 InboundEvent.ts 복사본(선택). reply_to는 스레드 root ts라 스레드 안 질문의 지연 계산에 못 쓴다.
  필드 추가 + 기본 None은 하위 호환이라 version은 1 그대로.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

Status = Literal["answered", "no_match", "error", "progress", "clarify"]
# progress: "검색 중…" 같은 중간 알림(어댑터가 직접 보냄) / clarify: 설비 되묻기(3주차 Step 9) — 본문이 질문, citations 비어 있음.
# 사용자의 스레드 답글이 같은 reply_to로 들어오면 워커가 원 질의와 합쳐 재검색한다(worker.ClarifyStore).


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = SCHEMA_VERSION

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes):
        return cls.model_validate_json(raw)

    @field_validator("ts", check_fields=False)
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("ts must be timezone-aware (e.g. 2026-10-29T09:00:00+09:00)")
        return v


class InboundEvent(_Event):
    """사용자 질문 1건."""

    event_id: str = Field(min_length=1, max_length=128, description="생산자가 정한 멱등 키. 재전송 시 동일")
    source: str = Field(min_length=1, description="어댑터 이름(slack/http/cli …). 등록 여부 검증은 라우터 몫")
    channel: str = Field(min_length=1, description="Slack 채널 ID, HTTP는 호출자 식별자, CLI는 'terminal'")
    user: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=2000, description="질문 원문. 앞뒤 공백 제거")
    ts: datetime = Field(default_factory=_utcnow, description="질문 발생 시각, timezone-aware")
    reply_to: str | None = Field(default=None, description="답을 달 스레드 ID(Slack thread_ts). 없으면 새 스레드")

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v

    def reply(self, text: str, citations: list[Citation] | None = None,
              status: Status = "answered", error: str | None = None) -> OutboundEvent:
        """이 질문에 대한 답 이벤트. channel/reply_to를 복사하고 in_reply_to를 채운다."""
        return OutboundEvent(
            in_reply_to=self.event_id, channel=self.channel,
            reply_to=self.reply_to or self.event_id, asked_at=self.ts,
            text=text, citations=citations or [], status=status, error=error,
        )


class Citation(BaseModel):
    """답변 근거 티켓 1건."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: str = Field(min_length=1, pattern=r"^INC-")
    score: float | None = Field(default=None, ge=-1.0, le=1.0, description="검색 유사도(cosine). 없으면 None")


class OutboundEvent(_Event):
    """질문 1건에 대한 답 1건."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    in_reply_to: str = Field(min_length=1, description="답하는 InboundEvent.event_id")
    channel: str = Field(min_length=1)
    reply_to: str | None = Field(default=None, description="답을 달 스레드 ID. inbound.reply_to 또는 inbound.event_id")
    text: str = Field(max_length=4000)
    citations: list[Citation] = Field(default_factory=list)
    status: Status
    error: str | None = Field(default=None, description="status=error일 때 사유")
    ts: datetime = Field(default_factory=_utcnow)
    asked_at: datetime | None = Field(default=None, description="질문 원본 시각(InboundEvent.ts). 지연 계산용, 선택")

    @field_validator("asked_at")
    @classmethod
    def _asked_at_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("asked_at must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _status_consistency(self) -> OutboundEvent:
        if self.status == "error" and not self.error:
            raise ValueError("status=error requires error")
        if self.status != "error" and self.error:
            raise ValueError("error is only allowed with status=error")
        if self.status == "answered" and not self.text.strip():
            raise ValueError("status=answered requires non-empty text")
        return self
