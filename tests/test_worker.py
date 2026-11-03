"""워커 통합 테스트 (RabbitMQ 필요, -m integration). 파이프라인은 fake."""
import asyncio
import json
import os
import threading
import time

import httpx
import pytest
import redis

from events import InboundEvent, OutboundEvent
from mq import topology
from mq.publisher import Publisher
from tests.test_mq import AMQP_URL, MGMT_URL, _broker_up, mgmt_queue, wait_depth
from worker.main import Worker

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not _broker_up(), reason="RabbitMQ not reachable")]

ALL_QUEUES = [topology.TASKS, topology.RESULTS, topology.DLQ] + [n for n, _ in topology.RETRY_TIERS]


def _mgmt(method: str, path: str, **kw):
    from urllib.parse import urlparse
    u = urlparse(MGMT_URL)
    base = f"{u.scheme}://{u.hostname}:{u.port or 15672}"
    r = httpx.request(method, base + path, auth=(u.username or "guest", u.password or "guest"), timeout=5, **kw)
    r.raise_for_status()
    return r


def purge_all():
    asyncio.run(_declare())
    for q in ALL_QUEUES:
        _mgmt("DELETE", f"/api/queues/%2F/{q}/contents")


async def _declare():
    pub = Publisher(AMQP_URL); await pub.connect(); await pub.close()


def peek(queue: str, count: int = 1) -> list[dict]:
    """메시지를 꺼내 보고 다시 넣는다(ack_requeue_true)."""
    return _mgmt("POST", f"/api/queues/%2F/{queue}/get",
                 json={"count": count, "ackmode": "ack_requeue_true", "encoding": "auto"}).json()


def publish(ev: InboundEvent, retry_count: int = 0):
    async def run():
        pub = Publisher(AMQP_URL); await pub.connect(); await pub.publish(ev, retry_count=retry_count); await pub.close()
    asyncio.run(run())


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _redis_up() -> bool:
    try:
        return redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_up(), reason=f"Redis not reachable at {REDIS_URL}")


def redis_client():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


class RunningWorker:
    def __init__(self, pipeline, redis_url="", **kw):
        # 기본 ""(가드 OFF): 멱등성 테스트만 redis_url을 준다
        self.w = Worker(pipeline, url=AMQP_URL, redis_url=redis_url, **kw)
        self.t = threading.Thread(target=lambda: asyncio.run(self.w.run()), daemon=True)

    def __enter__(self):
        self.t.start()
        assert self.w.ready.wait(10), "worker did not start"
        return self.w

    def __exit__(self, *a):
        self.w.request_stop()
        self.t.join(30)
        assert not self.t.is_alive(), "worker did not stop"


def wait_for(pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


EV = InboundEvent(event_id="Ev-worker-1", source="cli", channel="terminal", user="me", text="펌프다운이 오래 걸려요")


def test_a_success_moves_task_to_results():
    purge_all()
    seen = []

    def pipeline(inb: InboundEvent) -> OutboundEvent:
        seen.append(inb)
        return inb.reply("답 [INC-2026-0001]", status="answered")

    with RunningWorker(pipeline) as w:
        publish(EV)
        assert wait_for(lambda: w.processed == 1)
    assert seen == [EV]
    assert wait_depth(topology.RESULTS, 1) == 1
    assert wait_depth(topology.TASKS, 0) == 0
    [m] = peek(topology.RESULTS)
    out = OutboundEvent.from_json(m["payload"])
    assert out.in_reply_to == EV.event_id and out.status == "answered" and out.text == "답 [INC-2026-0001]"
    assert m["properties"]["headers"]["in_reply_to"] == EV.event_id and m["properties"]["delivery_mode"] == 2


def test_b_failure_goes_to_retry_5s_then_returns_to_tasks():
    purge_all()

    def boom(inb):
        raise RuntimeError("llm down")

    with RunningWorker(boom) as w:
        publish(EV)
        assert wait_for(lambda: w.failed == 1)
    # 워커가 멈춘 뒤: retry.5s에 1건, TTL 뒤 ai_tasks로 복귀(x-retry-count 1)
    assert wait_depth(topology.RETRY_TIERS[0][0], 1) == 1
    assert wait_depth(topology.TASKS, 1, timeout=20) == 1
    assert wait_depth(topology.RETRY_TIERS[0][0], 0) == 0
    [m] = peek(topology.TASKS)
    h = m["properties"]["headers"]
    assert h["x-retry-count"] == 1 and h["x-last-error"].startswith("RuntimeError: llm down")
    assert h["x-death"][0]["queue"] == topology.RETRY_TIERS[0][0] and h["x-death"][0]["reason"] == "expired"
    assert InboundEvent.from_json(m["payload"]) == EV
    assert wait_depth(topology.RESULTS, 0) == 0


def test_c_retries_exhausted_goes_to_dlq():
    """x-retry-count=3으로 들어온 메시지가 또 실패 → 4회째 → dlq (TTL 3단 155초를 기다리지 않고 마지막 단만 검증)."""
    purge_all()

    def boom(inb):
        raise ValueError("still broken")

    with RunningWorker(boom) as w:
        publish(EV, retry_count=topology.MAX_RETRIES)
        assert wait_for(lambda: w.failed == 1)
    assert wait_depth(topology.DLQ, 1) == 1
    assert wait_depth(topology.TASKS, 0) == 0
    for name, _ in topology.RETRY_TIERS:
        assert mgmt_queue(name)["messages"] == 0
    [m] = peek(topology.DLQ)
    assert InboundEvent.from_json(m["payload"]) == EV                       # 원문 보존
    assert m["properties"]["headers"]["x-retry-count"] == 4
    assert m["properties"]["headers"]["x-last-error"] == "ValueError: still broken"
    assert wait_depth(topology.RESULTS, 1) == 1                            # 사용자에게 실패 알림
    [r] = peek(topology.RESULTS)
    out = OutboundEvent.from_json(r["payload"])
    assert out.status == "error" and out.in_reply_to == EV.event_id and "still broken" in (out.error or "")
    assert r["properties"]["headers"]["status"] == "error"


def test_d_poison_message_goes_straight_to_dlq():
    purge_all()

    async def run():
        import aio_pika
        conn = await aio_pika.connect_robust(AMQP_URL)
        ch = await conn.channel()
        topo = await topology.declare(ch)
        await topo.events.publish(aio_pika.Message(body=b'{"not": "an event"}', message_id="poison-1"), routing_key=topology.TASKS)
        await conn.close()

    with RunningWorker(lambda inb: inb.reply("x")) as w:
        asyncio.run(run())
        assert wait_for(lambda: w.failed == 1)
    assert wait_depth(topology.DLQ, 1) == 1 and wait_depth(topology.TASKS, 0) == 0
    [m] = peek(topology.DLQ)
    assert m["payload"] == '{"not": "an event"}' and m["properties"]["headers"]["x-last-error"].startswith("poison:")


def test_e_graceful_stop_waits_for_inflight():
    purge_all()
    started = threading.Event()

    def slow(inb):
        started.set()
        time.sleep(3)
        return inb.reply("느린 답", status="answered")

    rw = RunningWorker(slow)
    with rw as w:
        publish(EV)
        assert started.wait(10)
        t0 = time.time()
        w.request_stop()
        rw.t.join(30)
        assert not rw.t.is_alive()
        assert time.time() - t0 >= 2.5          # 처리 중 1건을 기다렸다
        assert w.processed == 1
    assert wait_depth(topology.RESULTS, 1) == 1 and wait_depth(topology.TASKS, 0) == 0


# ---------- Step 5: 멱등성 ----------
@needs_redis
def test_f_duplicate_event_id_answered_once(caplog):
    purge_all()
    redis_client().delete(f"idem:{EV.event_id}")
    calls = []

    def pipeline(inb):
        calls.append(inb.event_id)
        return inb.reply("답", status="answered")

    with caplog.at_level("INFO", logger="worker"):
        with RunningWorker(pipeline, redis_url=REDIS_URL) as w:
            publish(EV)
            publish(EV)
            assert wait_for(lambda: w.processed + w.duplicates == 2)
    assert w.processed == 1 and w.duplicates == 1 and calls == [EV.event_id]
    assert caplog.text.count(f"duplicate, ack only: {EV.event_id}") == 1
    assert wait_depth(topology.RESULTS, 1) == 1 and wait_depth(topology.TASKS, 0) == 0
    r = redis_client()
    assert r.get(f"idem:{EV.event_id}") == "done"
    assert 86000 < r.ttl(f"idem:{EV.event_id}") <= 86400          # 성공 뒤에도 24h 유지


def test_g_redis_down_processes_with_warning(caplog):
    purge_all()

    with caplog.at_level("WARNING", logger="worker"):
        with RunningWorker(lambda inb: inb.reply("답", status="answered"),
                           redis_url="redis://127.0.0.1:1/0") as w:
            publish(EV)
            assert wait_for(lambda: w.processed == 1)
    assert w.duplicates == 0
    assert f"idempotency guard disabled for {EV.event_id}" in caplog.text
    assert wait_depth(topology.RESULTS, 1) == 1


@needs_redis
def test_h_failure_releases_key_so_retry_is_not_duplicate():
    purge_all()
    r = redis_client(); r.delete(f"idem:{EV.event_id}")

    def boom(inb):
        raise RuntimeError("llm down")

    with RunningWorker(boom, redis_url=REDIS_URL) as w:
        publish(EV)
        assert wait_for(lambda: w.failed == 1)
    assert w.duplicates == 0
    assert r.get(f"idem:{EV.event_id}") is None          # 키 삭제 → 5s 뒤 돌아오는 재시도가 처리된다
    purge_all()   # retry.5s에 남은 사본이 5s 뒤 ai_tasks로 돌아와 다음 테스트를 오염시키지 않게


@needs_redis
def test_i_stale_processing_key_is_reprocessed(caplog):
    """전 워커가 처리 중 죽은 흔적(processing, 오래됨)은 중복이 아니라 재처리."""
    purge_all()
    r = redis_client()
    r.set(f"idem:{EV.event_id}", f"processing:{int(time.time()) - 3600}", ex=86400)
    with caplog.at_level("WARNING", logger="worker"):
        with RunningWorker(lambda inb: inb.reply("답", status="answered"), redis_url=REDIS_URL) as w:
            publish(EV)
            assert wait_for(lambda: w.processed == 1)
    assert "stale processing key" in caplog.text and r.get(f"idem:{EV.event_id}") == "done"


@needs_redis
def test_j_fresh_processing_key_is_duplicate():
    """처리 중인 지 얼마 안 된 키(다른 워커가 지금 처리 중)는 중복으로 본다."""
    purge_all()
    r = redis_client()
    r.set(f"idem:{EV.event_id}", f"processing:{int(time.time())}", ex=86400)
    with RunningWorker(lambda inb: inb.reply("답", status="answered"), redis_url=REDIS_URL) as w:
        publish(EV)
        assert wait_for(lambda: w.duplicates == 1)
    assert w.processed == 0 and wait_depth(topology.RESULTS, 0) == 0


@needs_redis
def test_k_drain_timeout_releases_key_so_requeued_copy_is_reprocessed(caplog):
    """drain 타임아웃으로 버린 메시지: 브로커 requeue + 멱등 키 release → 재시작 워커가 300s 안에도 재처리한다."""
    purge_all()
    r = redis_client(); r.delete(f"idem:{EV.event_id}")
    started = threading.Event()

    def slow(inb):
        started.set()
        time.sleep(4)
        return inb.reply("느린 답", status="answered")

    rw = RunningWorker(slow, redis_url=REDIS_URL, drain_timeout=1.0)
    with caplog.at_level("INFO", logger="worker"):
        with rw as w:
            publish(EV)
            assert started.wait(10)
            assert (r.get(f"idem:{EV.event_id}") or "").startswith("processing:")
            w.request_stop()
            rw.t.join(30)
            assert not rw.t.is_alive()
    assert w.processed == 0
    assert "drain timeout: 1 message(s) left unacked" in caplog.text
    assert r.get(f"idem:{EV.event_id}") is None                       # 키 release
    assert wait_depth(topology.TASKS, 1) == 1 and wait_depth(topology.RESULTS, 0) == 0   # requeue, 결과 없음
    # 늦게 끝난 파이프라인 결과는 publish되지 않는다(루프 종료 시 cancel되거나 _abandoned로 버림) — RESULTS 0이 그 증거

    caplog.clear()
    with caplog.at_level("INFO", logger="worker"):
        with RunningWorker(lambda inb: inb.reply("답", status="answered"), redis_url=REDIS_URL) as w2:
            assert wait_for(lambda: w2.processed == 1)
    assert w2.duplicates == 0 and "duplicate, ack only" not in caplog.text
    assert wait_depth(topology.RESULTS, 1) == 1 and wait_depth(topology.TASKS, 0) == 0
    assert r.get(f"idem:{EV.event_id}") == "done"


def test_l_clarify_round_trip_rewrites_question_once(caplog):
    """되묻기(status=clarify) → 같은 스레드의 답 → 원 질의 + (설비: X)로 재검색. 답을 못 읽으면 원 질의를 되묻기 없이 답한다."""
    purge_all()
    r = redis_client()
    thread = "1793232753.000001"
    for eid in ("Ev-c1", "Ev-c2", "Ev-c3", "Ev-c4"):
        r.delete(f"idem:{eid}")
    r.delete(f"clarify:{thread}")
    seen = []

    def pipeline(inb, allow_clarify=True):
        seen.append((inb.event_id, inb.text, allow_clarify))
        if "(설비:" in inb.text or not allow_clarify:
            return inb.reply("답 [INC-2026-0001]", status="answered")
        return inb.reply("어느 설비에서 난 증상인가요?", status="clarify")

    q = InboundEvent(event_id="Ev-c1", source="slack_socket", channel="C1", user="U1", text="진공이 계속 떨어져요", reply_to=thread)
    a = InboundEvent(event_id="Ev-c2", source="slack_socket", channel="C1", user="U1", text="PECVD-03 이에요", reply_to=thread)
    with caplog.at_level("INFO", logger="worker"):
        with RunningWorker(pipeline, redis_url=REDIS_URL) as w:
            publish(q)
            assert wait_for(lambda: w.processed == 1)
            assert json.loads(r.get(f"clarify:{thread}"))["question"] == q.text and 0 < r.ttl(f"clarify:{thread}") <= 300
            publish(a)
            assert wait_for(lambda: w.processed == 2)
            assert r.get(f"clarify:{thread}") is None                       # pop — 왕복 1회
            # 다시 되묻기 → 답을 못 읽는 답글 → 원 질의를 allow_clarify=False로
            q2 = InboundEvent(event_id="Ev-c3", source="slack_socket", channel="C1", user="U1", text="히터 온도가 안 올라와요", reply_to=thread)
            x = InboundEvent(event_id="Ev-c4", source="slack_socket", channel="C1", user="U1", text="모르겠어요", reply_to=thread)
            publish(q2)
            assert wait_for(lambda: w.processed == 3)
            publish(x)
            assert wait_for(lambda: w.processed == 4)
    assert seen == [("Ev-c1", q.text, True), ("Ev-c2", q.text + " (설비: PECVD)", True),
                    ("Ev-c3", q2.text, True), ("Ev-c4", q2.text, False)]
    assert wait_depth(topology.RESULTS, 4) == 4
    outs = [OutboundEvent.from_json(m["payload"]) for m in peek(topology.RESULTS, 4)]
    assert [o.status for o in outs] == ["clarify", "answered", "clarify", "answered"]
    assert outs[1].in_reply_to == "Ev-c2" and outs[1].reply_to == thread   # 답은 답글 이벤트에, 같은 스레드에
    assert "clarify answered for Ev-c1" in caplog.text and "not understood" in caplog.text
