"""OpenAI 호환 LLM 클라이언트. 429/5xx 재시도, 선제 스로틀, JSON 파싱."""
import json
import os
import random
import re
import time

from openai import APIStatusError, OpenAI, RateLimitError

from . import config

_client: OpenAI | None = None
_judge: OpenAI | None = None
_last_call = 0.0
_last_judge_call = 0.0
MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "0"))   # 무료 티어면 6(초) 권장
FIXED_TEMPERATURE = os.getenv("LLM_FIXED_TEMPERATURE", "0") == "1"   # temperature 미지원 모델(gpt-5 계열 등)


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
         max_attempts: int = 6, *, judge: bool = False) -> str:
    """judge=True면 심판 제공사(JUDGE_*)로 호출."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    for attempt in range(max_attempts):
        if judge:
            _throttle_judge()
        else:
            _throttle()
        try:
            cl = judge_client() if judge else client()
            m = model or (config.JUDGE_MODEL if judge else config.LLM_MODEL)
            extra = {"temperature": temperature} if _supports_temperature(m) else {}
            resp = cl.chat.completions.create(model=m, messages=messages, **extra)
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


def judge_chat_json(user: str, system: str = "", temperature: float = 0.0):
    return parse_json(chat(user, system, None, temperature, judge=True))
