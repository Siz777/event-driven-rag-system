"""Slack Socket Mode 수신 어댑터 (slack-bolt AsyncApp).

흐름: app_mention / DM message → api.slack.to_inbound(source="slack_socket") → ai_tasks publish
      → 질문 스레드에 "검색 중…" progress 메시지(say thread_ts) → 그 메시지 ts를 Redis progress:{event_id}에(1h).
        slack_reply가 그 ts로 chat.update해 "검색 중…"이 답변으로 바뀐다. 키가 없으면(Redis 불가·만료) 새 답글.
- Socket Mode는 공개 URL·서명 검증이 필요 없다(앱 토큰 xapp-… 으로 WebSocket). Bolt가 3초 안에 envelope ack.
- 처리 대상이 아닌 이벤트(봇 글·편집·채널 일반 메시지)는 to_inbound가 None → 조용히 무시.
- publish 실패(브로커 단절)는 사용자에게 짧게 알리고 로그. Slack은 ack를 받았으므로 재전송하지 않는다 —
  이 경우 질문은 유실된다. 웹훅(503 → Slack 재전송)과 다른 점이며, 의도적으로 단순하게 둔다.
- 같은 event_id가 두 번 와도(Socket 재전송) 워커의 멱등 가드가 거른다. progress는 두 번 찍힐 수 있다.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable

from pydantic import ValidationError
from slack_bolt.async_app import AsyncApp

from adapters.progress import ProgressStore
from api import slack
from events import InboundEvent

log = logging.getLogger("adapters.slack_socket")

SOURCE = "slack_socket"
PROGRESS_TEXT = "검색 중… 유사 이력을 찾아 답을 정리하고 있습니다."
QUEUE_DOWN_TEXT = "지금은 질문을 접수할 수 없습니다(큐 연결 실패). 잠시 후 다시 멘션해 주세요."

PublishFn = Callable[[InboundEvent], Awaitable[None]]
SayFn = Callable[..., Awaitable[object]]


def allowed_channels() -> set[str]:
    """SLACK_ALLOWED_CHANNELS=C01,C02 (비우면 전부 허용)."""
    raw = os.getenv("SLACK_ALLOWED_CHANNELS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


async def handle_event(body: dict, say: SayFn, *, publish: PublishFn, allow: set[str] | None = None,
                       progress: ProgressStore | None = None) -> InboundEvent | None:
    """Bolt 이벤트 1건 처리. 테스트에서 Bolt 없이 직접 부른다. 돌려주는 값은 publish한 InboundEvent(무시했으면 None)."""
    try:
        ev = slack.to_inbound(body, source=SOURCE)
    except ValidationError as e:                     # 멘션만 보낸 경우 등
        log.warning("ignored invalid event %s: %s", body.get("event_id"), e.errors(include_url=False))
        return None
    if ev is None:
        return None
    if allow and ev.channel not in allow:
        log.info("ignored %s: channel %s not allowed", ev.event_id, ev.channel)
        return None

    t0 = time.monotonic()
    try:
        await publish(ev)
    except Exception as e:                            # 큐 장애. Socket Mode는 이미 ack됐으므로 사용자에게만 알린다
        log.error("publish failed for %s: %s: %s", ev.event_id, type(e).__name__, e)
        await say(text=QUEUE_DOWN_TEXT, thread_ts=ev.reply_to)
        return None
    res = await say(text=PROGRESS_TEXT, thread_ts=ev.reply_to)
    ts = _message_ts(res)
    stored = await progress.put(ev.event_id, ts) if (progress is not None and ts) else False
    log.info("inbound %s published + progress posted (%.0fms, ts=%s stored=%s) channel=%s thread=%s text=%r",
             ev.event_id, (time.monotonic() - t0) * 1000, ts, stored, ev.channel, ev.reply_to, ev.text[:60])
    return ev


def _message_ts(res: object) -> str | None:
    """say()의 응답(AsyncSlackResponse 또는 dict)에서 올린 메시지 ts."""
    try:
        return res.get("ts") if hasattr(res, "get") else res["ts"]      # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return None


def build_app(publish: PublishFn, *, bot_token: str | None = None, allow: set[str] | None = None,
              progress: ProgressStore | None = None) -> AsyncApp:
    """AsyncApp 구성. 토큰은 SLACK_BOT_TOKEN(xoxb-…). 서명 검증은 Socket Mode에서 쓰지 않으므로 끈다."""
    app = AsyncApp(token=bot_token or os.getenv("SLACK_BOT_TOKEN"), request_verification_enabled=False)
    allow = allowed_channels() if allow is None else allow

    @app.event("app_mention")
    async def _mention(body, say):
        await handle_event(body, say, publish=publish, allow=allow, progress=progress)

    @app.event("message")          # DM(channel_type=im)만 to_inbound가 통과시킨다. 나머지는 None
    async def _message(body, say):
        await handle_event(body, say, publish=publish, allow=allow, progress=progress)

    return app
