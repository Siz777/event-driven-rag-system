"""mq: 메시지 조립(유닛) + RabbitMQ 떠 있을 때만 도는 통합 테스트(-m integration)."""
import asyncio
import json
import os
import socket
import time
from urllib.parse import urlparse

import httpx
import pytest
from aio_pika import DeliveryMode

from events import InboundEvent
from mq import topology
from mq.publisher import DEFAULT_URL, Publisher, build_message

EV = InboundEvent(event_id="Ev-mq-1", source="cli", channel="terminal", user="me", text="펌프다운이 오래 걸려요")


def test_build_message_is_persistent_json_with_headers():
    m = build_message(EV, retry_count=2)
    assert m.delivery_mode == DeliveryMode.PERSISTENT
    assert m.content_type == "application/json"
    assert m.message_id == EV.event_id
    assert m.headers == {"x-retry-count": 2, "event_id": EV.event_id}
    assert InboundEvent.from_json(m.body) == EV


def test_retry_queue_for():
    assert topology.retry_queue_for(1) == "ai_tasks.retry.5s"
    assert topology.retry_queue_for(3) == "ai_tasks.retry.125s"
    assert topology.retry_queue_for(4) is None and topology.retry_queue_for(0) is None
    assert [ttl for _, ttl in topology.RETRY_TIERS] == [5_000, 25_000, 125_000]


# ---------- integration ----------
AMQP_URL = os.getenv("RABBITMQ_URL", DEFAULT_URL)
MGMT_URL = os.getenv("RABBITMQ_MGMT_URL", "http://guest:guest@localhost:15672")


def _broker_up() -> bool:
    u = urlparse(AMQP_URL)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 5672), timeout=0.5):
            return True
    except OSError:
        return False


integration = pytest.mark.skipif(not _broker_up(), reason=f"RabbitMQ not reachable at {AMQP_URL}")


def mgmt_queue(name: str) -> dict:
    u = urlparse(MGMT_URL)
    base = f"{u.scheme}://{u.hostname}:{u.port or 15672}"
    r = httpx.get(f"{base}/api/queues/%2F/{name}", auth=(u.username or "guest", u.password or "guest"), timeout=5)
    r.raise_for_status()
    return r.json()


def wait_depth(name: str, expected: int, timeout: float = 15.0) -> int:
    """관리 API 통계는 수 초 늦게 갱신된다 → 기대값이 나올 때까지 폴링."""
    deadline = time.time() + timeout
    depth = -1
    while time.time() < deadline:
        depth = mgmt_queue(name).get("messages", -1)
        if depth == expected:
            return depth
        time.sleep(0.5)
    return depth


@pytest.mark.integration
@integration
def test_publish_lands_in_ai_tasks():
    async def run():
        pub = Publisher(AMQP_URL)
        await pub.connect()
        await pub._topo.tasks.purge()
        await pub.publish(EV)
        await pub.close()

    asyncio.run(run())
    assert wait_depth(topology.TASKS, 1) == 1
    q = mgmt_queue(topology.TASKS)
    assert q["durable"] is True and q["arguments"]["x-dead-letter-exchange"] == topology.DLX


@pytest.mark.integration
@integration
def test_declare_is_idempotent_and_full_topology_exists():
    async def run():
        pub = Publisher(AMQP_URL)
        await pub.connect()
        await topology.declare(await pub._conn.channel())   # 두 번째 선언도 예외 없음
        await pub.close()

    asyncio.run(run())
    for name, ttl in topology.RETRY_TIERS:
        q = mgmt_queue(name)
        assert q["arguments"] == {"x-message-ttl": ttl, "x-dead-letter-exchange": topology.EXCHANGE,
                                  "x-dead-letter-routing-key": topology.TASKS}
    assert mgmt_queue(topology.DLQ)["durable"] and mgmt_queue(topology.RESULTS)["durable"]


@pytest.mark.integration
@integration
def test_webhook_end_to_end_depth_one():
    from fastapi.testclient import TestClient

    from api import slack
    from api.webhook import create_app

    secret = "it-secret"
    fx = json.load(open("tests/fixtures/slack_app_mention.json", encoding="utf-8"))
    body = json.dumps(fx, ensure_ascii=False).encode()
    ts = int(time.time())
    asyncio.run(_purge())
    with TestClient(create_app(signing_secret=secret, rabbitmq_url=AMQP_URL)) as client:
        r = client.post("/events", content=body, headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": slack.sign(secret, ts, body),
        })
    assert r.status_code == 202, r.text
    assert wait_depth(topology.TASKS, 1) == 1


async def _purge():
    pub = Publisher(AMQP_URL)
    await pub.connect()
    await pub._topo.tasks.purge()
    await pub.close()


def test_webhook_503_when_broker_unreachable():
    """브로커 포트가 닫혀 있으면 publish 예외 → 503 (fake 없이 실제 Publisher)."""
    from fastapi.testclient import TestClient

    from api import slack
    from api.webhook import create_app

    secret = "s"
    body = json.dumps(json.load(open("tests/fixtures/slack_app_mention.json", encoding="utf-8"))).encode()
    ts = int(time.time())
    with TestClient(create_app(signing_secret=secret, rabbitmq_url="amqp://guest:guest@127.0.0.1:1/")) as client:
        r = client.post("/events", content=body, headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": slack.sign(secret, ts, body),
        })
    assert r.status_code == 503
