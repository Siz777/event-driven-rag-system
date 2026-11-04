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


class FakeOK:
    """usage가 붙은 정상 응답. prompt/completion 토큰은 호출마다 (100, 10)."""
    def __init__(self):
        self.calls = 0

        class _C:
            def __init__(s2, outer): s2.outer = outer
            def create(s2, **kw):
                s2.outer.calls += 1
                usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 10})()
                msg = type("M", (), {"content": "ok"})()
                return type("R", (), {"usage": usage, "choices": [type("Ch", (), {"message": msg})()]})()
        self.chat = type("Chat", (), {"completions": _C(self)})()


def test_track_accumulates_tokens_and_cost(monkeypatch):
    monkeypatch.setattr(llm, "client", lambda: FakeOK())
    with llm.track(budget=0) as u:
        llm.chat("q", "s", model="gpt-5-mini")
        llm.chat("q", "s", model="gpt-5-mini")
    assert u.calls == 2 and u.prompt_tokens == 200 and u.completion_tokens == 20 and u.total_tokens == 220
    assert u.cost_usd() == round(200 * 0.25 / 1e6 + 20 * 2.0 / 1e6, 6) and "cost=$" in u.summary()
    assert u.by_model["gpt-5-mini"] == {"calls": 2, "prompt": 200, "completion": 20}
    with llm.track() as u2:
        llm.chat("q", "s", model="some-local-model")
    assert u2.cost_usd() is None and u2.summary().endswith("cost=?")
    assert llm.current_usage() is None                       # 블록 밖에서는 계측 없음


def test_token_budget_stops_further_calls(monkeypatch):
    fake = FakeOK()
    monkeypatch.setattr(llm, "client", lambda: fake)
    with llm.track(budget=150) as u:
        llm.chat("q", "s", model="gpt-5-mini")               # 누적 110 < 150 → 허용
        llm.chat("q", "s", model="gpt-5-mini")               # 호출 전 누적 110 < 150 → 허용, 뒤에 220
        with pytest.raises(llm.TokenBudgetExceeded):
            llm.chat("q", "s", model="gpt-5-mini")           # 호출 전 누적 220 ≥ 150 → 차단(호출 안 함)
    assert fake.calls == 2 and u.total_tokens == 220
