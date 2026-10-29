"""RabbitMQ 토폴로지. 선언 함수 하나(declare)로 멱등 — 같은 인자로 몇 번 불러도 안전.
RabbitMQ는 같은 이름·다른 인자로 선언하면 PRECONDITION_FAILED를 낸다. 인자를 바꿀 땐 큐를 지우고 다시 만든다.

  events (direct) ──"ai_tasks"──────────▶ ai_tasks ──nack/만료──▶ events.dlx ──"ai_tasks"──▶ ai_tasks.dlq
         ├──"ai_tasks.retry.5s"───▶ ai_tasks.retry.5s   ──TTL 5s──┐
         ├──"ai_tasks.retry.25s"──▶ ai_tasks.retry.25s  ──TTL 25s─┼─▶ events / "ai_tasks" (복귀)
         ├──"ai_tasks.retry.125s"─▶ ai_tasks.retry.125s ──TTL125s─┘
         ├──"ai_tasks.dlq"────────▶ ai_tasks.dlq   (워커가 재시도 소진 시 직접 publish)
         └──"ai_results"──────────▶ ai_results     (Step 4: OutboundEvent)

재시도는 워커가 명시적으로 한다(Step 6): 실패 → x-retry-count+1 → retry.{tier}로 publish → 원본 ack.
ai_tasks의 DLX는 워커가 손도 못 댄 경우(nack requeue=False, 큐 만료·초과)의 안전망.
"""
from __future__ import annotations

from dataclasses import dataclass

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

EXCHANGE = "events"
DLX = "events.dlx"
TASKS = "ai_tasks"
DLQ = "ai_tasks.dlq"
RESULTS = "ai_results"
RETRY_TIERS: list[tuple[str, int]] = [            # (큐 이름, TTL ms). 5s → 25s → 125s, 3회 넘으면 DLQ
    (f"{TASKS}.retry.5s", 5_000),
    (f"{TASKS}.retry.25s", 25_000),
    (f"{TASKS}.retry.125s", 125_000),
]
MAX_RETRIES = len(RETRY_TIERS)


def retry_queue_for(retry_count: int) -> str | None:
    """retry_count번째 실패 뒤 보낼 큐. 1 → 5s, 2 → 25s, 3 → 125s, 그 이상 None(DLQ)."""
    if 1 <= retry_count <= MAX_RETRIES:
        return RETRY_TIERS[retry_count - 1][0]
    return None


@dataclass
class Topology:
    events: AbstractExchange
    dlx: AbstractExchange
    tasks: AbstractQueue
    dlq: AbstractQueue
    results: AbstractQueue
    retry: dict[str, AbstractQueue]


async def declare(channel: AbstractChannel) -> Topology:
    events = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.DIRECT, durable=True)

    tasks = await channel.declare_queue(TASKS, durable=True, arguments={"x-dead-letter-exchange": DLX})
    await tasks.bind(events, routing_key=TASKS)

    dlq = await channel.declare_queue(DLQ, durable=True)
    await dlq.bind(dlx, routing_key=TASKS)        # ai_tasks에서 dead-letter된 건 (routing key는 원본 그대로)
    await dlq.bind(events, routing_key=DLQ)       # 워커가 직접 보내는 건

    retry: dict[str, AbstractQueue] = {}
    for name, ttl in RETRY_TIERS:
        q = await channel.declare_queue(name, durable=True, arguments={
            "x-message-ttl": ttl,
            "x-dead-letter-exchange": EXCHANGE,
            "x-dead-letter-routing-key": TASKS,
        })
        await q.bind(events, routing_key=name)
        retry[name] = q

    results = await channel.declare_queue(RESULTS, durable=True)
    await results.bind(events, routing_key=RESULTS)

    return Topology(events=events, dlx=dlx, tasks=tasks, dlq=dlq, results=results, retry=retry)
