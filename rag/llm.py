"""OpenAI 호환 LLM 클라이언트. 429/5xx 재시도, 선제 스로틀, JSON 파싱.

429는 두 종류라 다르게 다룬다(11-03 장애에서 배움: 크레딧 소진을 레이트리밋으로 알고 1,500번 재시도했다).
- rate_limit_exceeded: retry-after 만큼 기다렸다 재시도.
- insufficient_quota(credit_balance_exhausted 등): 기다려도 소용없다 → 로그 찍고 즉시 실패. 워커는 재시도 티어 → DLQ → error 응답.

토큰·비용·지연 계측(11-04): `with track(budget) as u:` 안의 chat() 호출은 u(Usage)에 호출 수·prompt/completion 토큰·초·모델별 합계를
쌓는다. contextvar라 질의마다 독립이고, 스레드 풀로 나가는 호출은 호출자가 copy_context()로 넘겨야 한다(graph.grade가 그렇게 함).
LLM_TOKEN_BUDGET(질의당 총 토큰, 0=무제한): 한도를 넘긴 뒤의 chat()은 TokenBudgetExceeded — 폭주(무한 되묻기·긴 컨텍스트) 가드.
비용은 PRICE_PER_MTOK(모델 → (입력, 출력) USD/1M 토큰) 표로 추정. 모르는 모델은 0으로 두고 로그에 '?'.
"""
import contextvars
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field

from openai import APIStatusError, OpenAI, RateLimitError

from . import config

log = logging.getLogger("rag.llm")
NON_RETRYABLE_429 = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached", "billing_not_active"}

_client: OpenAI | None = None
_judge: OpenAI | None = None
_last_call = 0.0
_last_judge_call = 0.0
MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "0"))   # 무료 티어면 6(초) 권장
FIXED_TEMPERATURE = os.getenv("LLM_FIXED_TEMPERATURE", "0") == "1"   # temperature 미지원 모델(gpt-5 계열 등)
TOKEN_BUDGET = int(os.getenv("LLM_TOKEN_BUDGET", "0"))                # 질의당 총 토큰 한도(0=무제한)
# USD / 1M tokens (입력, 출력). 2026-11 공개가 기준 — 바뀌면 여기만. 로컬(Ollama·vLLM)은 0.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40), "gpt-5": (1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.60), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
}
if os.getenv("LLM_PRICE_IN_PER_MTOK") and os.getenv("LLM_PRICE_OUT_PER_MTOK"):   # 미등록 모델·사내 단가 덮어쓰기
    PRICE_PER_MTOK[os.getenv("LLM_MODEL", "")] = (float(os.environ["LLM_PRICE_IN_PER_MTOK"]), float(os.environ["LLM_PRICE_OUT_PER_MTOK"]))


class TokenBudgetExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    budget: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_usd(self) -> float | None:
        """모델 단가표로 추정. 어느 모델이든 단가를 모르면 None."""
        total = 0.0
        for m, t in self.by_model.items():
            price = PRICE_PER_MTOK.get(m) or PRICE_PER_MTOK.get(m.split(":")[0])
            if price is None:
                return None
            total += t["prompt"] * price[0] / 1e6 + t["completion"] * price[1] / 1e6
        return round(total, 6)

    def record(self, model: str, prompt: int, completion: int, seconds: float) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.seconds += seconds
            d = self.by_model.setdefault(model, {"calls": 0, "prompt": 0, "completion": 0})
            d["calls"] += 1; d["prompt"] += prompt; d["completion"] += completion

    def as_dict(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens, "seconds": round(self.seconds, 2), "cost_usd": self.cost_usd(),
                "by_model": self.by_model, "budget": self.budget}

    def summary(self) -> str:
        c = self.cost_usd()
        return (f"calls={self.calls} tokens={self.prompt_tokens}+{self.completion_tokens}={self.total_tokens} "
                f"llm_sec={self.seconds:.1f} cost=${c:.4f}" if c is not None else
                f"calls={self.calls} tokens={self.prompt_tokens}+{self.completion_tokens}={self.total_tokens} llm_sec={self.seconds:.1f} cost=?")


_usage: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("llm_usage", default=None)


class track:
    """`with track(budget) as u:` — 블록 안 chat() 호출의 토큰·비용·지연을 u에 모은다. 중첩되면 안쪽이 이긴다."""

    def __init__(self, budget: int | None = None):
        self.usage = Usage(budget=TOKEN_BUDGET if budget is None else budget)

    def __enter__(self) -> Usage:
        self._tok = _usage.set(self.usage)
        return self.usage

    def __exit__(self, *exc) -> None:
        _usage.reset(self._tok)


def current_usage() -> Usage | None:
    return _usage.get()


def _check_budget(u: Usage | None, model: str) -> None:
    if u is not None and u.budget and u.total_tokens >= u.budget:
        raise TokenBudgetExceeded(f"LLM token budget {u.budget} exceeded after {u.calls} calls ({u.total_tokens} tokens) before {model}")


def _record(u: Usage | None, model: str, resp, t0: float) -> None:
    if u is None:
        return
    usage = getattr(resp, "usage", None)
    u.record(model, int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0), time.monotonic() - t0)


def _supports_temperature(model: str) -> bool:
    """gpt-5 계열은 temperature를 받지 않음(기본값 고정). LLM_FIXED_TEMPERATURE=1로 강제 가능."""
    return not (FIXED_TEMPERATURE or model.startswith("gpt-5"))


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "none")
    return _client


def judge_client() -> OpenAI:
    global _judge
    if _judge is None:
        _judge = OpenAI(base_url=config.JUDGE_BASE_URL, api_key=config.JUDGE_API_KEY or "none")
    return _judge


def _retry_after(e: Exception) -> float | None:
    try:
        v = e.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(v) if v else None
    except Exception:
        return None


def _error_code(e: Exception) -> tuple[str, str]:
    """429/4xx 본문의 (type, code). 없으면 ("", "")."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body) if isinstance(body.get("error", body), dict) else body
        return str(err.get("type") or ""), str(err.get("code") or "")
    return "", ""


def _throttle() -> None:
    global _last_call
    if MIN_INTERVAL > 0:
        gap = MIN_INTERVAL - (time.time() - _last_call)
        if gap > 0:
            time.sleep(gap)
    _last_call = time.time()


def _throttle_judge() -> None:
    global _last_judge_call
    if config.JUDGE_MIN_INTERVAL > 0:
        gap = config.JUDGE_MIN_INTERVAL - (time.time() - _last_judge_call)
        if gap > 0:
            time.sleep(gap)
    _last_judge_call = time.time()


def chat(user: str, system: str = "", model: str | None = None, temperature: float = 0.2,
         max_attempts: int = 6, *, judge: bool = False, timeout: float | None = None) -> str:
    """judge=True면 심판 제공사(JUDGE_*)로 호출. timeout은 요청 1회의 초 단위 제한(None이면 SDK 기본 10분)."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    return complete(messages, model=model, temperature=temperature, max_attempts=max_attempts, judge=judge, timeout=timeout).content or ""


def complete(messages: list[dict], *, model: str | None = None, temperature: float = 0.2, max_attempts: int = 6,
             judge: bool = False, timeout: float | None = None, tools: list[dict] | None = None, tool_choice: str | dict = "auto"):
    """chat.completions 1회(재시도 포함) → 응답 message(content·tool_calls). tools를 주면 OpenAI tool calling —
    호출자가 message.tool_calls를 실행하고 {"role": "tool", "tool_call_id", "content"}를 붙여 다시 부른다(rag/graph.py generate).
    토큰 계측(track)·예산 가드·429 분류는 chat과 같다."""
    u = _usage.get()
    for attempt in range(max_attempts):
        if judge:
            _throttle_judge()
        else:
            _throttle()
        try:
            cl = judge_client() if judge else client()
            m = model or (config.JUDGE_MODEL if judge else config.LLM_MODEL)
            _check_budget(u, m)
            extra = {"temperature": temperature} if _supports_temperature(m) else {}
            if timeout is not None:
                extra["timeout"] = timeout
            if tools:
                extra["tools"] = tools
                extra["tool_choice"] = tool_choice
            t0 = time.monotonic()
            resp = cl.chat.completions.create(model=m, messages=messages, **extra)
            _record(u, m, resp, t0)
            return resp.choices[0].message
        except TokenBudgetExceeded:
            raise
        except RateLimitError as e:                       # 429
            etype, code = _error_code(e)
            log.warning("429 from %s: type=%s code=%s (attempt %d/%d)", m, etype or "?", code or "?", attempt + 1, max_attempts)
            if etype in NON_RETRYABLE_429 or code in NON_RETRYABLE_429:
                raise                                     # 크레딧 소진: 기다려도 소용없다
            wait = _retry_after(e) or min(60.0, 2 ** attempt + random.uniform(0, 1))
        except APIStatusError as e:
            if e.status_code < 500:                       # 400·401·404: 재시도 무의미
                raise
            wait = min(30.0, 2 ** attempt + random.uniform(0, 1))
        except Exception as e:                            # 네트워크 끊김 등
            wait = min(30.0, 2 ** attempt + random.uniform(0, 1))
            print(f"[warn] {type(e).__name__}: {e}")
        print(f"[retry {attempt + 1}/{max_attempts}] {wait:.0f}s 대기")
        time.sleep(wait)
    raise RuntimeError("LLM 호출 재시도 한도 초과")


def parse_json(text: str):
    """코드펜스·앞뒤 설명을 벗겨내고 JSON만 파싱."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
    return json.loads(text[start:])


def chat_json(user: str, system: str = "", model: str | None = None, temperature: float = 0.2):
    return parse_json(chat(user, system, model, temperature))


def judge_chat_json(user: str, system: str = "", temperature: float = 0.0):
    return parse_json(chat(user, system, None, temperature, judge=True))
