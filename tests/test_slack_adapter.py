"""Slack 어댑터 유닛 테스트 (Bolt·브로커·Slack 없이: handle_event·format_reply·ReplyConsumer._on_message를 fake로)."""
import asyncio
import json
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError

from adapters import slack_reply, slack_socket
from adapters.slack_reply import ReplyConsumer, format_reply
from adapters.slack_socket import PROGRESS_TEXT, QUEUE_DOWN_TEXT, handle_event
from api import slack
from events import Citation, InboundEvent, OutboundEvent

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "slack_app_mention.json").read_text())


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self, fail: Exception | None = None):
        self.calls, self.fail = [], fail

    async def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        if self.fail:
            raise self.fail


# ---- to_inbound 재사용 ----
def test_to_inbound_socket_source_and_thread():
    ev = slack.to_inbound(FIXTURE, source="slack_socket")
    assert ev.source == "slack_socket" and ev.event_id == "Ev08ABC123"
    assert ev.reply_to == "1793232753.123456"                       # 새 질문 → 메시지 ts가 스레드
    p = json.loads(json.dumps(FIXTURE)); p["event"]["thread_ts"] = "1700000000.000001"
    assert slack.to_inbound(p).reply_to == "1700000000.000001"      # 스레드 안 질문 → 그 스레드


# ---- handle_event ----
def test_handle_event_publishes_then_progress():
    pub, say = Recorder(), Recorder()
    ev = run(handle_event(FIXTURE, say, publish=pub))
    assert isinstance(ev, InboundEvent) and ev.source == "slack_socket"
    assert len(pub.calls) == 1 and pub.calls[0][0][0] == ev
    assert say.calls == [((), {"text": PROGRESS_TEXT, "thread_ts": "1793232753.123456"})]


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(bot_id="B1"),                                  # 봇(자기 progress 메시지 포함)
    lambda e: e.update(subtype="message_changed"),
    lambda e: e.update(type="message", channel_type="channel"),       # 채널 일반 메시지
    lambda e: e.update(text="<@U0789BOT>"),                           # 멘션만 → ValidationError → 무시
])
def test_handle_event_ignores(mutate):
    p = json.loads(json.dumps(FIXTURE)); mutate(p["event"])
    pub, say = Recorder(), Recorder()
    assert run(handle_event(p, say, publish=pub)) is None
    assert pub.calls == [] and say.calls == []


def test_handle_event_dm_allowed():
    p = json.loads(json.dumps(FIXTURE))
    p["event"].update(type="message", channel_type="im", channel="D0123DM", text="압력이 자꾸 튀는데")
    pub, say = Recorder(), Recorder()
    ev = run(handle_event(p, say, publish=pub))
    assert ev.channel == "D0123DM" and ev.text == "압력이 자꾸 튀는데"


def test_handle_event_channel_allowlist():
    pub, say = Recorder(), Recorder()
    assert run(handle_event(FIXTURE, say, publish=pub, allow={"C_OTHER"})) is None
    assert pub.calls == [] and say.calls == []
    assert run(handle_event(FIXTURE, say, publish=pub, allow={"C0123FAB"})) is not None


def test_handle_event_publish_failure_tells_user_no_progress():
    pub, say = Recorder(fail=ConnectionError("broker down")), Recorder()
    assert run(handle_event(FIXTURE, say, publish=pub)) is None
    assert say.calls == [((), {"text": QUEUE_DOWN_TEXT, "thread_ts": "1793232753.123456"})]


# ---- format_reply ----
def _out(status, text="본문 [INC-2026-0003]", citations=(), error=None):
    return OutboundEvent(in_reply_to="Ev1", channel="C1", reply_to="1793232753.123456", text=text,
                         citations=[Citation(ticket_id=t, score=0.7) for t in citations], status=status, error=error)


def test_format_answered_has_body_and_citation_footer():
    s = format_reply(_out("answered", citations=["INC-2026-0003", "INC-2026-0002"]))
    assert s.startswith("본문 [INC-2026-0003]") and s.endswith("근거 티켓: INC-2026-0003, INC-2026-0002")


def test_format_answered_without_citations_has_no_footer():
    assert format_reply(_out("answered")) == "본문 [INC-2026-0003]"


def test_format_no_match_phrase():
    assert format_reply(_out("no_match", text="")) == slack_reply.NO_MATCH_TEXT
    s = format_reply(_out("no_match", text="설비명을 알려주세요"))
    assert s.startswith("유사 이력 없음") and s.endswith("설비명을 알려주세요")
    assert format_reply(_out("no_match", text="유사 이력 없음. 다시 물어봐 주세요")) == slack_reply.NO_MATCH_TEXT


def test_format_error_short_with_reason():
    s = format_reply(_out("error", text="", error="TimeoutError: LLM 90s"))
    assert s.startswith(slack_reply.ERROR_TEXT) and "TimeoutError" in s and len(s) < 200


def test_format_progress_passthrough():
    assert format_reply(_out("progress", text="검색 중…")) == "검색 중…"


def test_format_clarify_is_the_question_itself():
    q = "어느 설비에서 난 증상인가요?\n• PECVD(CVD) — 예: [INC-2026-0001]"
    assert format_reply(_out("clarify", text=q)) == q


# ---- ReplyConsumer._on_message: ack/nack 규칙 ----
class FakeMsg:
    def __init__(self, body: bytes):
        self.body, self.message_id, self.acked, self.nacked = body, "m1", False, None

    async def ack(self): self.acked = True
    async def nack(self, requeue=True): self.nacked = requeue


def _consume(post, body: bytes) -> FakeMsg:
    c = ReplyConsumer(post, url="amqp://unused")
    m = FakeMsg(body)
    run(c._on_message(m))
    return m


def test_consumer_posts_then_acks(monkeypatch):
    post = Recorder()
    m = _consume(post, _out("answered", citations=["INC-2026-0003"]).to_json().encode())
    assert m.acked and m.nacked is None
    (channel, thread_ts, text), _ = post.calls[0]
    assert channel == "C1" and thread_ts == "1793232753.123456" and text.endswith("근거 티켓: INC-2026-0003")


def test_consumer_latency_uses_asked_at_not_thread_root(caplog):
    """스레드 root가 10분 전이어도 질문이 3초 전이면 지연은 ~3s (2주차 664.9s 문제)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    root_ts = f"{(now - timedelta(minutes=10)).timestamp():.6f}"
    out = OutboundEvent(in_reply_to="Ev1", channel="C1", reply_to=root_ts, text="답", status="answered",
                        asked_at=now - timedelta(seconds=3))
    with caplog.at_level("INFO", logger="adapters.slack_reply"):
        _consume(Recorder(), out.to_json().encode())
    note = [r.message for r in caplog.records if "latency_from_question=" in r.message][0]
    latency = float(note.split("latency_from_question=")[1].split("s")[0])
    assert 2.5 <= latency < 10 and "(from reply_to)" not in note
    # asked_at 없는 구버전 메시지는 reply_to 폴백 + 표시
    caplog.clear()
    with caplog.at_level("INFO", logger="adapters.slack_reply"):
        _consume(Recorder(), OutboundEvent(in_reply_to="Ev1", channel="C1", reply_to=root_ts, text="답",
                                          status="answered").to_json().encode())
    note = [r.message for r in caplog.records if "latency_from_question=" in r.message][0]
    assert "(from reply_to)" in note and float(note.split("latency_from_question=")[1].split("s")[0]) > 500


def test_consumer_poison_acked_and_dropped():
    post = Recorder()
    m = _consume(post, b'{"nope": 1}')
    assert m.acked and post.calls == []


def test_consumer_permanent_slack_error_acked():
    err = SlackApiError("x", {"ok": False, "error": "channel_not_found"})
    m = _consume(Recorder(fail=err), _out("answered").to_json().encode())
    assert m.acked and m.nacked is None


def test_consumer_transient_error_requeued(monkeypatch):
    monkeypatch.setattr(slack_reply, "TRANSIENT_BACKOFF_SEC", 0)
    err = SlackApiError("x", {"ok": False, "error": "ratelimited"})
    m = _consume(Recorder(fail=err), _out("answered").to_json().encode())
    assert not m.acked and m.nacked is True
    m2 = _consume(Recorder(fail=OSError("net")), _out("answered").to_json().encode())
    assert not m2.acked and m2.nacked is True


# ---- progress ts: slack_socket 저장 → slack_reply chat.update ----
class FakeStore:
    """ProgressStore 대역(메모리). get/put/delete 시그니처 동일."""
    def __init__(self, data=None, fail=False):
        self.data, self.fail = dict(data or {}), fail

    async def put(self, event_id, ts):
        if self.fail: return False
        self.data[event_id] = ts; return True

    async def get(self, event_id):
        return None if self.fail else self.data.get(event_id)

    async def delete(self, event_id):
        self.data.pop(event_id, None)


class SayWithTs(Recorder):
    async def __call__(self, *a, **kw):
        await super().__call__(*a, **kw)
        return {"ok": True, "ts": "1793263940.000500"}


class Poster(Recorder):
    """post(channel, thread_ts, text) + update(channel, ts, text) 대역."""
    def __init__(self, update_fail: Exception | None = None):
        super().__init__(); self.updates, self.update_fail = [], update_fail

    async def update(self, channel, ts, text):
        self.updates.append((channel, ts, text))
        if self.update_fail: raise self.update_fail


def test_handle_event_stores_progress_ts():
    store = FakeStore()
    ev = run(handle_event(FIXTURE, SayWithTs(), publish=Recorder(), progress=store))
    assert store.data == {ev.event_id: "1793263940.000500"}


def test_handle_event_without_ts_or_store_still_ok():
    assert run(handle_event(FIXTURE, Recorder(), publish=Recorder(), progress=FakeStore())) is not None   # say가 ts 안 줌
    store = FakeStore(fail=True)                                                                           # Redis 불가
    assert run(handle_event(FIXTURE, SayWithTs(), publish=Recorder(), progress=store)) is not None and store.data == {}


def _consume2(poster, store, out) -> FakeMsg:
    c = ReplyConsumer(poster, progress=store, url="amqp://unused")
    m = FakeMsg(out.to_json().encode()); run(c._on_message(m)); return m, c


def test_consumer_updates_progress_message_and_deletes_key():
    poster, store = Poster(), FakeStore({"Ev1": "1793263940.000500"})
    m, c = _consume2(poster, store, _out("answered", citations=["INC-2026-0003"]))
    assert m.acked and c.updated == 1 and poster.calls == []
    assert poster.updates == [("C1", "1793263940.000500", "본문 [INC-2026-0003]\n\n근거 티켓: INC-2026-0003")]
    assert store.data == {}


def test_consumer_posts_when_no_progress_key():
    poster, store = Poster(), FakeStore()
    m, c = _consume2(poster, store, _out("answered"))
    assert m.acked and c.updated == 0 and poster.updates == [] and len(poster.calls) == 1


def test_consumer_falls_back_to_post_when_progress_message_gone():
    err = SlackApiError("x", {"ok": False, "error": "message_not_found"})
    poster, store = Poster(update_fail=err), FakeStore({"Ev1": "1793263940.000500"})
    m, c = _consume2(poster, store, _out("answered"))
    assert m.acked and len(poster.updates) == 1 and len(poster.calls) == 1 and c.updated == 0


def test_consumer_update_transient_error_requeues(monkeypatch):
    monkeypatch.setattr(slack_reply, "TRANSIENT_BACKOFF_SEC", 0)
    err = SlackApiError("x", {"ok": False, "error": "ratelimited"})
    poster, store = Poster(update_fail=err), FakeStore({"Ev1": "1793263940.000500"})
    m, c = _consume2(poster, store, _out("answered"))
    assert not m.acked and m.nacked is True and poster.calls == [] and store.data   # 키 유지 → 재시도 때 다시 update
