"""이벤트 계약 직렬화 왕복: dict → model → JSON → model 동일."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from events import Citation, InboundEvent, OutboundEvent

KST = timezone(timedelta(hours=9))

INBOUND = {
    "event_id": "Ev08ABC123",
    "source": "slack",
    "channel": "C0123FAB",
    "user": "U0456ENG",
    "text": "펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?",
    "ts": "2026-10-29T09:12:33.123456+09:00",
    "reply_to": None,
}

OUTBOUND = {
    "event_id": "a1b2c3",
    "in_reply_to": "Ev08ABC123",
    "channel": "C0123FAB",
    "reply_to": "Ev08ABC123",
    "text": "유사 사례 2건 [INC-2026-0012] [INC-2026-0015] …",
    "citations": [{"ticket_id": "INC-2026-0012", "score": 0.71}, {"ticket_id": "INC-2026-0015", "score": 0.66}],
    "status": "answered",
    "ts": "2026-10-29T00:12:40Z",
}


@pytest.mark.parametrize("model, payload", [(InboundEvent, INBOUND), (OutboundEvent, OUTBOUND)])
def test_roundtrip_dict_model_json_model(model, payload):
    m1 = model.model_validate(payload)
    raw = m1.to_json()
    m2 = model.from_json(raw)
    assert m1 == m2
    assert m1.model_dump() == m2.model_dump()
    # JSON 안에서도 ts는 ISO 8601, version은 1
    doc = json.loads(raw)
    assert doc["version"] == 1
    assert datetime.fromisoformat(doc["ts"]) == m1.ts


def test_inbound_ts_keeps_offset_and_microseconds():
    m = InboundEvent.model_validate(INBOUND)
    assert m.ts.utcoffset() == timedelta(hours=9)
    assert m.ts.microsecond == 123456
    assert InboundEvent.from_json(m.to_json()).ts == m.ts


def test_inbound_defaults():
    m = InboundEvent(event_id="x", source="cli", channel="terminal", user="me", text="  압력 튐  ")
    assert m.text == "압력 튐"
    assert m.reply_to is None
    assert m.ts.tzinfo is not None
    assert m.version == 1


@pytest.mark.parametrize("bad", [
    {"ts": "2026-10-29T09:12:33"},        # naive
    {"text": "   "},                       # blank
    {"text": ""},
    {"event_id": ""},
    {"source": ""},                        # blank source
    {"version": 2},                        # unknown schema version
    {"unknown_field": 1},                  # extra forbidden
])
def test_inbound_rejects(bad):
    with pytest.raises(ValidationError):
        InboundEvent.model_validate({**INBOUND, **bad})


def test_inbound_is_frozen():
    m = InboundEvent.model_validate(INBOUND)
    with pytest.raises(ValidationError):
        m.text = "변경"


def test_reply_copies_routing_fields():
    inb = InboundEvent.model_validate(INBOUND)
    out = inb.reply("답", citations=[Citation(ticket_id="INC-2026-0001", score=0.5)])
    assert out.in_reply_to == inb.event_id
    assert out.channel == inb.channel
    assert out.reply_to == inb.event_id            # inbound.reply_to 없으면 inbound.event_id가 스레드
    assert out.status == "answered"
    assert out.citations[0].ticket_id == "INC-2026-0001"
    assert OutboundEvent.from_json(out.to_json()) == out

    inb2 = InboundEvent.model_validate({**INBOUND, "reply_to": "1730000000.000100"})
    assert inb2.reply("답").reply_to == "1730000000.000100"


def test_outbound_status_rules():
    base = {k: v for k, v in OUTBOUND.items() if k not in ("status", "text", "citations")}
    OutboundEvent(**base, status="no_match", text="유사 이력 없음")
    OutboundEvent(**base, status="error", text="", error="LLM timeout")
    with pytest.raises(ValidationError):
        OutboundEvent(**base, status="error", text="")              # error 사유 없음
    with pytest.raises(ValidationError):
        OutboundEvent(**base, status="answered", text="", error="x")  # answered에 error
    with pytest.raises(ValidationError):
        OutboundEvent(**base, status="answered", text="   ")       # answered인데 빈 답


@pytest.mark.parametrize("bad", [
    {"ticket_id": "TKT-1"},
    {"ticket_id": "INC-2026-0001", "score": 1.5},
    {"ticket_id": "INC-2026-0001", "extra": 1},
])
def test_citation_rejects(bad):
    with pytest.raises(ValidationError):
        Citation.model_validate(bad)
