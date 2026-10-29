"""progress 메시지 ts 저장소 (Redis). slack_socket이 "검색 중…"을 올린 뒤 그 ts를 두고, slack_reply가 꺼내 chat.update한다.

- 키 progress:{event_id} → ts, TTL 1h. 답이 1시간 넘게 안 오면 새 메시지로 단다(update 대상이 없으니).
- Redis 불가면 None/False로 조용히 폴백(WARNING 1줄) — progress 편집은 UX이지 정합성이 아니다. 없으면 postMessage.
"""
from __future__ import annotations

import asyncio
import logging
import os

import redis.asyncio as aioredis

log = logging.getLogger("adapters.progress")

TTL_SEC = 3600


class ProgressStore:
    def __init__(self, url: str | None = None):
        self.url = url if url is not None else (os.getenv("REDIS_URL") or None)
        self._r: aioredis.Redis | None = None
        if self.url:
            self._r = aioredis.from_url(self.url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)

    @staticmethod
    def key(event_id: str) -> str:
        return f"progress:{event_id}"

    async def put(self, event_id: str, ts: str) -> bool:
        if self._r is None:
            return False
        try:
            await self._r.set(self.key(event_id), ts, ex=TTL_SEC)
            return True
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as e:
            log.warning("progress ts for %s not stored (redis unavailable: %s)", event_id, type(e).__name__)
            return False

    async def get(self, event_id: str) -> str | None:
        if self._r is None:
            return None
        try:
            return await self._r.get(self.key(event_id))
        except (aioredis.RedisError, OSError, asyncio.TimeoutError) as e:
            log.warning("progress ts for %s not read (redis unavailable: %s)", event_id, type(e).__name__)
            return None

    async def delete(self, event_id: str) -> None:
        if self._r is None:
            return
        try:
            await self._r.delete(self.key(event_id))
        except (aioredis.RedisError, OSError, asyncio.TimeoutError):
            pass

    async def close(self) -> None:
        if self._r is not None:
            await self._r.aclose()
