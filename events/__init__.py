"""이벤트 계약. 어댑터(Slack/HTTP) ↔ 큐 ↔ 워커 사이의 유일한 데이터 형식."""
from .contracts import SCHEMA_VERSION, Citation, InboundEvent, OutboundEvent

__all__ = ["SCHEMA_VERSION", "Citation", "InboundEvent", "OutboundEvent"]
