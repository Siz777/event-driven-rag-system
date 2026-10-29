"""Slack Events API 처리: 서명 검증 + 페이로드 → InboundEvent 변환.

서명: https://api.slack.com/authentication/verifying-requests-from-slack
  base = "v0:{X-Slack-Request-Timestamp}:{raw body}"
  X-Slack-Signature = "v0=" + HMAC-SHA256(signing_secret, base).hexdigest()
  타임스탬프가 현재와 5분 넘게 차이 나면 재전송 공격으로 보고 거부.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime, timezone

from events import InboundEvent

VERSION = "v0"
MAX_SKEW_SEC = 5 * 60
_MENTION = re.compile(r"<@[A-Z0-9]+>")

# 처리하는 inner event 종류. message는 DM(channel_type=im)만.
HANDLED_EVENTS = {"app_mention", "message"}


def sign(secret: str, ts: str | int, body: bytes) -> str:
    base = f"{VERSION}:{ts}:".encode() + body
    return VERSION + "=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify(secret: str, ts: str | None, signature: str | None, body: bytes, *, now: float | None = None) -> str | None:
    """통과하면 None, 실패하면 사유 문자열."""
    if not ts or not signature:
        return "missing signature headers"
    try:
        ts_val = int(ts)
    except ValueError:
        return "bad timestamp"
    if abs((now if now is not None else time.time()) - ts_val) > MAX_SKEW_SEC:
        return "stale timestamp"
    if not hmac.compare_digest(sign(secret, ts, body), signature):
        return "signature mismatch"
    return None


def _ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def to_inbound(payload: dict, *, source: str = "slack") -> InboundEvent | None:
    """event_callback → InboundEvent. 처리 대상이 아니면 None (봇 메시지, 서브타입, 미지 타입, DM 아닌 message).
    변환은 하되 검증은 InboundEvent에 맡긴다 — 빈 텍스트 등은 ValidationError로 올라간다.
    reply_to는 thread_ts(스레드 안 질문) 또는 메시지 ts(새 질문) — 답은 항상 질문 스레드에 단다."""
    ev = payload.get("event") or {}
    kind = ev.get("type")
    if kind not in HANDLED_EVENTS:
        return None
    if ev.get("bot_id") or ev.get("subtype"):           # 봇·편집·삭제·파일 공유 등은 질문이 아님
        return None
    if kind == "message" and ev.get("channel_type") != "im":
        return None                                      # 채널 일반 메시지는 멘션으로만 받는다
    text = _MENTION.sub("", ev.get("text", ""))
    return InboundEvent(
        event_id=payload.get("event_id", ""),
        source=source,
        channel=ev.get("channel", ""),
        user=ev.get("user", ""),
        text=text,
        ts=_ts_to_datetime(ev["ts"]) if ev.get("ts") else datetime.now(timezone.utc),
        reply_to=ev.get("thread_ts") or ev.get("ts"),
    )
