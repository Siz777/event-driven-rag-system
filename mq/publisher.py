"""aio-pika publisher. connect_robust(끊기면 자동 재연결) + publisher confirms + persistent 메시지.

publish()는 브로커가 confirm할 때까지 기다린다. 라우팅 안 되면(mandatory) DeliveryError, 연결 불가면 예외 —
웹훅은 이 예외를 503으로 바꾼다. 연결은 첫 publish 때 지연 생성(브로커가 죽어 있어도 웹훅 프로세스는 뜬다).
"""
from __future__ import annotations

import asyncio
import logging
import os

import aio_pika
from aio_pika import DeliveryMode, Message

from events import InboundEvent
from mq import topology

log = logging.getLogger("mq.publisher")

DEFAULT_URL = "amqp://guest:guest@localhost:5672/"


def build_message(ev: InboundEvent, *, retry_count: int = 0) -> Message:
    return Message(
        body=ev.to_json().encode(),
        content_type="application/json",
        content_encoding="utf-8",
        delivery_mode=DeliveryMode.PERSISTENT,
        message_id=ev.event_id,
        timestamp=ev.ts,
        headers={"x-retry-count": retry_count, "event_id": ev.event_id},
    )


class Publisher:
    def __init__(self, url: str | None = None):
        self.url = url or os.getenv("RABBITMQ_URL", DEFAULT_URL)
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._topo: topology.Topology | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._topo is not None:
                return
            self._conn = await aio_pika.connect_robust(self.url, timeout=5)
            channel = await self._conn.channel(publisher_confirms=True)
            self._topo = await topology.declare(channel)
            log.info("connected to %s, topology declared", self.url.split("@")[-1])

    async def publish(self, ev: InboundEvent, *, routing_key: str = topology.TASKS, retry_count: int = 0) -> None:
        if self._topo is None:
            await self.connect()
        assert self._topo is not None
        await self._topo.events.publish(build_message(ev, retry_count=retry_count), routing_key=routing_key, mandatory=True)
        log.info("published %s -> %s (retry %d)", ev.event_id, routing_key, retry_count)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn, self._topo = None, None
