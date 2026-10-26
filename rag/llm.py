"""OpenAI 호환 LLM 클라이언트. 429/5xx 재시도, 선제 스로틀, JSON 파싱."""
import json
import os
import random
import re
import time

from openai import APIStatusError, OpenAI, RateLimitError

from . import config

_client: OpenAI | None = None
_last_call = 0.0
MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "0"))   # 무료 티어면 6(초) 권장


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "none")
    return _client


def _retry_after(e: Exception) -> float | None:
    try:
        v = e.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(v) if v else None
    except Exception:
        return None


def _throttle() -> None:
    global _last_call
    if MIN_INTERVAL > 0:
        gap = MIN_INTERVAL - (time.time() - _last_call)
        if gap > 0:
            time.sleep(gap)
    _last_call = time.time()


def chat(user: str, system: str = "", model: str | None = None, temperature: float = 0.2,
         max_attempts: int = 6) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    for attempt in range(max_attempts):
        _throttle()
        try:
            resp = client().chat.completions.create(
                model=model or config.LLM_MODEL, messages=messages, temperature=temperature)
            return resp.choices[0].message.content or ""
        except RateLimitError as e:                       # 429
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
