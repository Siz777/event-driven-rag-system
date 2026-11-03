"""rag.llm.chat의 429 처리: 크레딧 소진은 즉시 실패, 레이트리밋은 재시도."""
import httpx
import pytest
from openai import RateLimitError

from rag import llm


def _rl(body: dict) -> RateLimitError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return RateLimitError("429", response=httpx.Response(429, request=req), body=body)


class FakeClient:
    def __init__(self, exc):
        self.calls = 0
        self.exc = exc

        class _C:
            def __init__(s2, outer): s2.outer = outer
            def create(s2, **kw):
                s2.outer.calls += 1
                raise s2.outer.exc
        self.chat = type("Chat", (), {"completions": _C(self)})()


def test_insufficient_quota_fails_immediately_without_retry(monkeypatch, caplog):
    fake = FakeClient(_rl({"error": {"message": "You have no credits remaining.", "type": "insufficient_quota",
                                     "code": "credit_balance_exhausted"}}))
    monkeypatch.setattr(llm, "client", lambda: fake)
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    with caplog.at_level("WARNING", logger="rag.llm"):
        with pytest.raises(RateLimitError):
            llm.chat("q", "s", max_attempts=6)
    assert fake.calls == 1 and slept == []
    assert "type=insufficient_quota code=credit_balance_exhausted" in caplog.text


def test_rate_limit_exceeded_retries_then_gives_up(monkeypatch, caplog):
    fake = FakeClient(_rl({"error": {"message": "slow down", "type": "requests", "code": "rate_limit_exceeded"}}))
    monkeypatch.setattr(llm, "client", lambda: fake)
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    with caplog.at_level("WARNING", logger="rag.llm"):
        with pytest.raises(RuntimeError, match="재시도 한도 초과"):
            llm.chat("q", "s", max_attempts=3)
    assert fake.calls == 3 and len(slept) == 3
    assert "code=rate_limit_exceeded" in caplog.text
