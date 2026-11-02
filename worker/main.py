"""aio-pika 워커: ai_tasks 1건 → InboundEvent → pipeline.answer → OutboundEvent → ai_results → ack.

- prefetch 1, 수동 ack. ack은 결과 publish(confirm)까지 끝난 뒤에만.
- 실패: x-retry-count+1 → retry 티어 큐(5s/25s/125s)로 publish → 원본 ack. 3회 넘으면 ai_tasks.dlq.
  nack(requeue=True)는 쓰지 않는다(같은 실패를 즉시 반복, 횟수 셀 수 없음).
- 본문이 InboundEvent로 안 읽히는 독 메시지는 재시도 없이 바로 dlq.
- 재시도/dlq publish 자체가 실패하면(브로커 단절) ack도 못 하므로 그대로 둔다 — 연결이 끊기면 브로커가 requeue.
- 파이프라인은 동기(임베딩·LLM)라 to_thread로 돌린다. 이벤트 루프를 막으면 AMQP heartbeat가 끊겨 연결이 죽는다.
- graceful shutdown: SIGTERM/SIGINT → stop 이벤트 → consumer cancel(새 메시지 중단) → 처리 중 1건 완료 대기(timeout) → 연결 종료.
- 멱등성(Step 5): 수신 직후 Redis `SET idem:{event_id} processing:{epoch} NX EX 86400`.
  이미 있으면 "duplicate, ack only" — 파이프라인·ai_results publish 없이 ack. 성공하면 값을 done으로(TTL 유지).
  실패(재시도·dlq로 보냄)면 키 삭제 — 재시도 메시지는 같은 event_id라 키가 남아 있으면 영원히 중복 취급된다.
  처리 중 죽으면 processing 값이 남는다 → 중복 판정 때 processing이 STALE_PROCESSING_SEC보다 오래됐으면 새로 처리.
  drain 타임아웃으로 처리 중 메시지를 버리고 종료할 때는 그 event_id의 키를 release한다 — 브로커가 requeue한 사본을
  다음 워커가 300초 안에 받아도 중복으로 버리지 않게(3주차 Step 0 실험에서 발견한 유실 창).
  Redis 연결 실패면 WARNING 찍고 가드 없이 처리 — 가드 때문에 서비스가 멈추지 않게.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import signal
import threading
import time
from typing import Callable

import aio_pika
import redis.asyncio as aioredis
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractIncomingMessage
from dotenv import load_dotenv
from pydantic import ValidationError

from events import InboundEvent, OutboundEvent
from mq import topology
from mq.publisher import DEFAULT_URL, build_message

log = logging.getLogger("worker")

IDEM_TTL_SEC = 24 * 3600
STALE_PROCESSING_SEC = float(os.getenv("WORKER_STALE_PROCESSING_SEC", "300"))   # LLM 90s + drain 120s 보다 길게


class IdempotencyGuard:
    """Redis SET NX 기반. 모든 메서드는 Redis 장애 때 예외 대신 None/False를 돌려주고 WARNING을 남긴다."""

    def __init__(self, url: str | None):
        self.url = url
        self._r: aioredis.Redis | None = None
        if url:
            self._r = aioredis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)

    @staticmethod
    def key(event_id: str) -> str:
        return f"idem:{event_id}"

    async def acquire(self, event_id: str) -> bool | None:
        """True=처음 본 event_id(처리하라), False=중복, None=Redis 불가(가드 없이 처리)."""
        if self._r is None:
            return None
        k = self.key(event_id)
        try:
            if await self._r.set(k, f"processing:{time.time():.0f}", nx=True, ex=IDEM_TTL_SEC):
                return True
            cur = await self._r.get(k) or ""
            if cur.startswith("processing:"):
                age = time.time() - float(cur.split(":", 1)[1] or 0)
                if age > STALE_PROCESSING_SEC:          # 전 워커가 처리 중 죽은 흔적 → 다시 처리
                    log.warning("stale processing key for %s (%.0fs old): reprocessing", event_id, age)
                    await self._r.set(k, f"processing:{time.time():.0f}", ex=IDEM_TTL_SEC)
                    return True
            return False
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as e:
            log.warning("idempotency guard disabled for %s (redis unavailable: %s)", event_id, type(e).__name__)
            return None

    async def mark_done(self, event_id: str) -> None:
        if self._r is None:
            return
        try:
            await self._r.set(self.key(event_id), "done", ex=IDEM_TTL_SEC)
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as e:
            log.warning("could not mark %s done (%s)", event_id, type(e).__name__)

    async def release(self, event_id: str) -> None:
        if self._r is None:
            return
        try:
            await self._r.delete(self.key(event_id))
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as e:
            log.warning("could not release %s (%s)", event_id, type(e).__name__)

    async def close(self) -> None:
        if self._r is not None:
            await self._r.aclose()

PipelineFn = Callable[[InboundEvent], OutboundEvent]


class Worker:
    def __init__(self, pipeline: PipelineFn, *, url: str | None = None, redis_url: str | None = None,
                 drain_timeout: float = 120.0, prefetch: int = 1, warmup: Callable[[], None] | None = None):
        self.pipeline = pipeline
        self.url = url or os.getenv("RABBITMQ_URL", DEFAULT_URL)
        self.guard = IdempotencyGuard(redis_url if redis_url is not None else os.getenv("REDIS_URL") or None)
        self.drain_timeout = drain_timeout
        self.prefetch = prefetch
        self.warmup = warmup
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: set[asyncio.Task] = set()
        self._inflight_ids: dict[asyncio.Task, str] = {}   # 처리 중 태스크 → event_id (drain 타임아웃 때 키 release용)
        self._abandoned: set[str] = set()                   # drain 타임아웃으로 버린 event_id — 늦게 끝나도 결과를 내지 않는다
        self.processed = 0
        self.failed = 0
        self.duplicates = 0
        self.ready = threading.Event()      # consume 시작 뒤 set (테스트·운영 헬스용)

    # ---- 제어 ----
    def request_stop(self) -> None:
        """다른 스레드에서도 안전."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        else:
            self._stop.set()

    def _install_signals(self) -> None:
        assert self._loop is not None
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):   # 스레드 안의 루프 등
                pass

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("signal %s received: stop requested", sig.name)
        self._stop.set()

    # ---- 실행 ----
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_signals()
        if self.warmup is not None:
            log.info("warmup (embedding·vector DB load)…")
            await asyncio.to_thread(self.warmup)
            log.info("warmup done")

        conn = await aio_pika.connect_robust(self.url, timeout=5)
        channel = await conn.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=self.prefetch)
        self._topo = await topology.declare(channel)
        tag = await self._topo.tasks.consume(self._on_message, no_ack=False)
        log.info("consuming %s (prefetch %d), url %s, idempotency %s", topology.TASKS, self.prefetch,
                 self.url.split("@")[-1], "redis " + self.guard.url.split("@")[-1] if self.guard.url else "OFF")
        self.ready.set()
        if os.getenv("WORKER_READY_FILE"):           # 컨테이너 healthcheck용
            pathlib.Path(os.environ["WORKER_READY_FILE"]).touch()

        await self._stop.wait()
        log.info("stopping: cancelling consumer (no new messages)")
        await self._topo.tasks.cancel(tag)
        if self._inflight:
            log.info("draining %d in-flight (timeout %.0fs)…", len(self._inflight), self.drain_timeout)
            done, pending = await asyncio.wait(set(self._inflight), timeout=self.drain_timeout)
            if pending:
                ids = [self._inflight_ids[t] for t in pending if t in self._inflight_ids]
                self._abandoned.update(ids)
                for eid in ids:
                    await self.guard.release(eid)
                log.warning("drain timeout: %d message(s) left unacked — idempotency key released for %s; "
                            "broker will requeue on disconnect", len(pending), ids)
            else:
                log.info("draining done")
        await conn.close()
        await self.guard.close()
        log.info("connection closed. processed=%d failed=%d duplicates=%d", self.processed, self.failed, self.duplicates)

    async def _on_message(self, msg: AbstractIncomingMessage) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._inflight.add(task)
        try:
            await self._handle(msg)
        finally:
            self._inflight.discard(task)
            self._inflight_ids.pop(task, None)

    async def _handle(self, msg: AbstractIncomingMessage) -> None:
        headers = dict(msg.headers or {})
        retry_count = int(headers.get("x-retry-count", 0) or 0)
        try:
            inbound = InboundEvent.from_json(msg.body)
        except (ValidationError, ValueError) as e:
            log.error("poison message %s → dlq: %s", msg.message_id, e)
            await self._publish_raw(msg, topology.DLQ, {**headers, "x-last-error": f"poison: {e}"[:500]})
            await msg.ack()
            self.failed += 1
            return

        first = await self.guard.acquire(inbound.event_id)
        if first is False:
            self.duplicates += 1
            log.info("duplicate, ack only: %s", inbound.event_id)
            await msg.ack()
            return

        task = asyncio.current_task()
        if task is not None:
            self._inflight_ids[task] = inbound.event_id
        log.info("processing %s (retry %d) text=%r", inbound.event_id, retry_count, inbound.text[:60])
        t0 = time.monotonic()
        try:
            out = await asyncio.to_thread(self.pipeline, inbound)
            if inbound.event_id in self._abandoned:
                log.info("abandoned %s finished after drain timeout — result dropped, broker requeued the task", inbound.event_id)
                return
            await self._topo.events.publish(
                Message(body=out.to_json().encode(), content_type="application/json", content_encoding="utf-8",
                        delivery_mode=DeliveryMode.PERSISTENT, message_id=out.event_id, correlation_id=inbound.event_id,
                        headers={"event_id": out.event_id, "in_reply_to": inbound.event_id, "status": out.status}),
                routing_key=topology.RESULTS, mandatory=True)
            await msg.ack()
            await self.guard.mark_done(inbound.event_id)
            self.processed += 1
            log.info("done %s → %s status=%s citations=%d (%.1fs)", inbound.event_id, topology.RESULTS,
                     out.status, len(out.citations), time.monotonic() - t0)
        except Exception as e:
            if inbound.event_id in self._abandoned:        # 연결이 이미 닫힌 뒤 끝난 경우: 키는 release됐고 브로커가 requeue
                log.info("abandoned %s failed after drain timeout (%s) — ignored", inbound.event_id, type(e).__name__)
                return
            self.failed += 1
            await self.guard.release(inbound.event_id)     # 재시도 메시지(같은 event_id)가 중복으로 안 걸리게
            nxt = retry_count + 1
            target = topology.retry_queue_for(nxt) or topology.DLQ
            log.warning("failed %s (retry %d→%d) → %s: %s: %s", inbound.event_id, retry_count, nxt, target,
                        type(e).__name__, e)
            m = build_message(inbound, retry_count=nxt)
            m.headers["x-last-error"] = f"{type(e).__name__}: {e}"[:500]
            await self._topo.events.publish(m, routing_key=target, mandatory=True)
            await msg.ack()

    async def _publish_raw(self, msg: AbstractIncomingMessage, routing_key: str, headers: dict) -> None:
        await self._topo.events.publish(
            Message(body=msg.body, content_type=msg.content_type, delivery_mode=DeliveryMode.PERSISTENT,
                    message_id=msg.message_id, headers=headers),
            routing_key=routing_key, mandatory=True)


def _real_pipeline() -> tuple[PipelineFn, Callable[[], None]]:
    from rag import embed
    from rag.index import client
    from rag.pipeline import answer

    def warmup() -> None:
        embed.embed(["warmup"], kind="query")
        client()

    return answer, warmup


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pipeline, warmup = _real_pipeline()
    asyncio.run(Worker(pipeline, warmup=warmup, drain_timeout=float(os.getenv("WORKER_DRAIN_TIMEOUT", "120"))).run())


if __name__ == "__main__":
    main()
