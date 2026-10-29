"""POST /events: 슬랙 서명 검증, url_verification, event_callback → InboundEvent."""
import copy
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import slack
from api.webhook import create_app
from events import InboundEvent

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "slack_app_mention.json").read_text())


def signed(body: bytes, *, secret: str = SECRET, ts: int | None = None, sig: str | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": sig or slack.sign(secret, ts, body),
    }


@pytest.fixture
def ctx():
    captured: list[InboundEvent] = []

    async def publish(ev: InboundEvent):
        captured.append(ev)

    return TestClient(create_app(signing_secret=SECRET, publish=publish)), captured


def post(client, payload: dict | bytes, **kw):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    return client.post("/events", content=body, headers=signed(body, **kw))


def test_app_mention_accepted_and_converted(ctx):
    client, captured = ctx
    r = post(client, FIXTURE)
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted", "event_id": "Ev08ABC123"}
    assert len(captured) == 1
    ev = captured[0]
    assert ev.source == "slack"
    assert ev.event_id == "Ev08ABC123"
    assert ev.channel == "C0123FAB" and ev.user == "U0456ENG"
    assert ev.text == "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?"   # 멘션 제거 + strip
    assert ev.ts.tzinfo is not None and ev.ts.timestamp() == pytest.approx(1793232753.123456, abs=1e-6)
    assert ev.reply_to is None
    assert InboundEvent.from_json(ev.to_json()) == ev


def test_thread_reply_keeps_thread_ts(ctx):
    client, captured = ctx
    p = copy.deepcopy(FIXTURE)
    p["event"]["thread_ts"] = "1761696000.000100"
    assert post(client, p).status_code == 202
    assert captured[0].reply_to == "1761696000.000100"


def test_url_verification_echoes_challenge(ctx):
    client, captured = ctx
    r = post(client, {"type": "url_verification", "token": "x", "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"})
    assert r.status_code == 200
    assert r.json() == {"challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"}
    assert captured == []


@pytest.mark.parametrize("kw, reason", [
    ({"secret": "wrong-secret"}, "signature mismatch"),
    ({"sig": "v0=deadbeef"}, "signature mismatch"),
    ({"ts": int(time.time()) - 600}, "stale timestamp"),
    ({"ts": int(time.time()) + 600}, "stale timestamp"),
])
def test_bad_signature_401(ctx, kw, reason):
    client, captured = ctx
    r = post(client, FIXTURE, **kw)
    assert r.status_code == 401 and r.json()["error"] == reason
    assert captured == []


def test_missing_headers_401(ctx):
    client, _ = ctx
    r = client.post("/events", content=json.dumps(FIXTURE).encode(), headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_tampered_body_401(ctx):
    client, captured = ctx
    body = json.dumps(FIXTURE).encode()
    headers = signed(body)
    tampered = body.replace(b"Ev08ABC123", b"Ev08ABC999")
    r = client.post("/events", content=tampered, headers=headers)
    assert r.status_code == 401 and captured == []


def test_invalid_json_400(ctx):
    client, _ = ctx
    assert post(client, b"{not json").status_code == 400


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(bot_id="B0123BOT"),            # 봇이 쓴 글
    lambda e: e.update(subtype="message_changed"),    # 편집
    lambda e: e.update(type="reaction_added"),        # 미지 타입
    lambda e: e.update(type="message", channel_type="channel"),   # DM 아닌 일반 메시지
])
def test_ignored_events_200_without_publish(ctx, mutate):
    client, captured = ctx
    p = copy.deepcopy(FIXTURE)
    mutate(p["event"])
    r = post(client, p)
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert captured == []


def test_dm_message_accepted(ctx):
    client, captured = ctx
    p = copy.deepcopy(FIXTURE)
    p["event"].update(type="message", channel_type="im", channel="D0123DM", text="압력이 자꾸 튀는데")
    assert post(client, p).status_code == 202
    assert captured[0].channel == "D0123DM" and captured[0].text == "압력이 자꾸 튀는데"


def test_mention_only_ignored_with_log(ctx, caplog):
    client, captured = ctx
    p = copy.deepcopy(FIXTURE)
    p["event"]["text"] = "<@U0789BOT>"
    with caplog.at_level("WARNING", logger="api.webhook"):
        r = post(client, p)
    assert r.status_code == 200 and r.json() == {"status": "ignored", "reason": "invalid event"}
    assert captured == []
    assert "ignored invalid event Ev08ABC123" in caplog.text and "text" in caplog.text


def test_publish_failure_503():
    async def boom(ev):
        raise ConnectionError("rabbit down")

    client = TestClient(create_app(signing_secret=SECRET, publish=boom))
    assert post(client, FIXTURE).status_code == 503


def test_no_secret_configured_503():
    client = TestClient(create_app(signing_secret=None))
    assert post(client, FIXTURE).status_code == 503


def test_verify_helper_directly():
    body = b'{"a":1}'
    ts = 1_761_696_753
    sig = slack.sign(SECRET, ts, body)
    assert slack.verify(SECRET, str(ts), sig, body, now=ts + 10) is None
    assert slack.verify(SECRET, str(ts), sig, body, now=ts + 301) == "stale timestamp"
    assert slack.verify(SECRET, "abc", sig, body, now=ts) == "bad timestamp"
    assert slack.verify(SECRET, None, sig, body, now=ts) == "missing signature headers"
