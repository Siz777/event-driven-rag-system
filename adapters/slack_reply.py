"""ai_results 소비자: OutboundEvent → chat.postMessage(channel, thread_ts=reply_to) → ack.

- progress:{in_reply_to} 키(Redis, slack_socket이 둔 "검색 중…" 메시지 ts)가 있으면 chat.update로 그 메시지를 답으로 바꾼다.
  없거나 update가 실패하면(message_not_found·삭제됨) postMessage로 새 답글. 성공하면 키 삭제.
- 전송 성공 뒤에만 ack. Slack API 일시 오류(레이트리밋·네트워크)는 nack(requeue) + 잠깐 대기 — 답은 이미 만들어졌으니 버리지 않는다.
- 영구 오류(channel_not_found, not_in_channel, invalid_auth 등)는 재시도해도 같으므로 로그 남기고 ack(폐기).
- 본문이 OutboundEvent로 안 읽히면 독 메시지: 로그 + ack.
- 상태 없음: reply_to(thread_ts)·channel이 메시지 안에 있어 inbound 조회가 필요 없다 → 언제든 재시작 가능.
- 지연 로그: asked_at(질문 원본 ts) 기준으로 "질문 → 스레드 답" 왕복 시간을 계산한다. asked_at이 없으면(구버전 메시지)
  reply_to(Slack ts, epoch)로 폴백 — 스레드 안 질문은 root 기준이라 부정확.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from adapters.progress import ProgressStore
from events import OutboundEvent
from mq import topology
from mq.publisher import DEFAULT_URL

log = logging.getLogger("adapters.slack_reply")

NO_MATCH_TEXT = "유사 이력 없음 — 과거 티켓에서 비슷한 사례를 찾지 못했습니다. 설비명·증상·알람 코드를 넣어 다시 물어봐 주세요."
ERROR_TEXT = "답변 생성에 실패했습니다. 잠시 후 다시 질문해 주세요."
PERMANENT_ERRORS = {"channel_not_found", "not_in_channel", "invalid_auth", "account_inactive", "token_revoked",
                    "is_archived", "msg_too_long", "no_text", "invalid_arguments"}
TRANSIENT_BACKOFF_SEC = float(os.getenv("SLACK_REPLY_BACKOFF", "2"))

PostFn = Callable[[str, str | None, str], Awaitable[None]]   # (channel, thread_ts, text)
UpdateFn = Callable[[str, str, str], Awaitable[None]]          # (channel, ts, text)


def format_reply(out: OutboundEvent) -> str:
    if out.status == "answered":
        ids = [c.ticket_id for c in out.citations]
        footer = f"\n\n근거 티켓: {', '.join(ids)}" if ids else ""
        return out.text + footer
    if out.status == "no_match":
        body = out.text.strip()
        return NO_MATCH_TEXT if not body or body.startswith("유사 이력 없음") else f"{NO_MATCH_TEXT}\n\n{body}"
    if out.status == "error":
        return f"{ERROR_TEXT} (사유: {(out.error or '')[:80]})"
    if out.status == "clarify":            # 설비 되묻기(Step 9): 본문이 질문. 사용자는 이 스레드에 멘션으로 답한다
        return out.text
    return out.text or "처리 중…"          # progress


class SlackPoster:
    def __init__(self, token: str | None = None):
        self.client = AsyncWebClient(token=token or os.getenv("SLACK_BOT_TOKEN"))

    async def __call__(self, channel: str, thread_ts: str | None, text: str) -> None:
        await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    async def update(self, channel: str, ts: str, text: str) -> None:
        await self.client.chat_update(channel=channel, ts=ts, text=text)


class ReplyConsumer:
    def __init__(self, post: PostFn, *, update: UpdateFn | None = None, progress: ProgressStore | None = None,
                 url: str | None = None, prefetch: int = 4):
        self.post = post
        self.update = update if update is not None else getattr(post, "update", None)
        self.progress = progress
        self.updated = 0
        self.url = url or os.getenv("RABBITMQ_URL", DEFAULT_URL)
        self.prefetch = prefetch
        self._stop = asyncio.Event()
        self.posted = 0
        self.dropped = 0
        self.ready = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        conn = await aio_pika.connect_robust(self.url, timeout=5)
        channel = await conn.channel()
        await channel.set_qos(prefetch_count=self.prefetch)
        topo = await topology.declare(channel)
        tag = await topo.results.consume(self._on_message, no_ack=False)
        log.info("consuming %s (prefetch %d), url %s", topology.RESULTS, self.prefetch, self.url.split("@")[-1])
        self.ready.set()
        await self._stop.wait()
        log.info("stopping: cancelling consumer")
        await topo.results.cancel(tag)
        await conn.close()
        log.info("connection closed. posted=%d (updated %d) dropped=%d", self.posted, self.updated, self.dropped)

    async def _on_message(self, msg: AbstractIncomingMessage) -> None:
        try:
            out = OutboundEvent.from_json(msg.body)
        except (ValidationError, ValueError) as e:
            log.error("poison result %s dropped: %s", msg.message_id, e)
            self.dropped += 1
            await msg.ack()
            return
        text = format_reply(out)
        try:
            how = await self._deliver(out, text)
        except SlackApiError as e:
            code = (e.response or {}).get("error", "") if hasattr(e, "response") else ""
            if code in PERMANENT_ERRORS:
                log.error("dropped %s (in_reply_to %s): slack %s", out.event_id, out.in_reply_to, code)
                self.dropped += 1
                await msg.ack()
                return
            log.warning("slack transient error for %s: %s — requeue after %.0fs", out.in_reply_to, code or e,
                        TRANSIENT_BACKOFF_SEC)
            await asyncio.sleep(TRANSIENT_BACKOFF_SEC)
            await msg.nack(requeue=True)
            return
        except Exception as e:                          # 네트워크 등
            log.warning("post failed for %s: %s: %s — requeue after %.0fs", out.in_reply_to, type(e).__name__, e,
                        TRANSIENT_BACKOFF_SEC)
            await asyncio.sleep(TRANSIENT_BACKOFF_SEC)
            await msg.nack(requeue=True)
            return
        await msg.ack()
        self.posted += 1
        if self.progress is not None:
            await self.progress.delete(out.in_reply_to)
        log.info("%s %s status=%s citations=%d channel=%s thread=%s%s", how, out.in_reply_to, out.status,
                 len(out.citations), out.channel, out.reply_to, _latency_note(out))

    async def _deliver(self, out: OutboundEvent, text: str) -> str:
        """progress 메시지가 있으면 update, 아니면 post. 돌려주는 값은 로그용 'updated'/'posted'."""
        ts = await self.progress.get(out.in_reply_to) if self.progress is not None else None
        if ts and self.update is not None:
            try:
                await self.update(out.channel, ts, text)
                self.updated += 1
                return "updated"
            except SlackApiError as e:
                code = (e.response or {}).get("error", "") if hasattr(e, "response") else ""
                if code not in {"message_not_found", "cant_update_message", "edit_window_closed"}:
                    raise
                log.warning("progress message %s for %s gone (%s) — posting new reply", ts, out.in_reply_to, code)
        await self.post(out.channel, out.reply_to, text)
        return "posted"


def _latency_note(out: OutboundEvent) -> str:
    """질문 → 답글 왕복 시간. asked_at 우선, 없으면 reply_to(Slack 메시지 ts, epoch 초) 폴백."""
    if out.asked_at is not None:
        return f" latency_from_question={time.time() - out.asked_at.timestamp():.1f}s"
    try:
        return f" latency_from_question={time.time() - float(out.reply_to or ''):.1f}s (from reply_to)"
    except ValueError:
        return ""
