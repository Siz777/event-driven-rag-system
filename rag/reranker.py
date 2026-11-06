"""로컬 cross-encoder 판정 (Step 10, GRADE_MODE=cross_encoder). (질문, 티켓 본문+최종 원인) 쌍 → 관련 확률 → ≥ 임계값이면 yes.

- 모델: RERANKER_MODEL(기본: domains/<domain>/artifacts/reranker/best 가 있으면 그것, 없으면 BAAI/bge-reranker-v2-m3 제로샷).
- 임계값: RERANKER_THRESHOLD, 없으면 모델 폴더의 manifest.json(threshold), 그것도 없으면 0.5.
- 쌍의 티켓 쪽 텍스트는 API 판정 프롬프트와 같은 재료(doc_text + "최종 원인: …") — 두 판정이 같은 정보를 본다.
- 외부 전송 0. 메모리: bge-reranker-v2-m3 fp32 ≈ 2.2GB(bge-m3와 함께 올리면 워커 RSS ≈ 5GB대 — 11-04 측정치는 일지).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path

from . import config
from .retrieve import Hit

log = logging.getLogger("rag.reranker")
_LOCK = threading.Lock()   # MPS/torch 추론은 스레드 동시 호출에 안전하지 않다(평가 스크립트가 질의를 병렬로 돌림)

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
ARTIFACT_DIR = config.DOMAIN_DIR / "artifacts" / "reranker"
def _max_length() -> int:
    """RERANKER_MAX_LENGTH, 없으면 manifest(max_length: 학습 때 쓴 값), 그것도 없으면 512."""
    env = os.getenv("RERANKER_MAX_LENGTH")
    if env:
        return int(env)
    mf = Path(model_path()) / "manifest.json"
    if mf.exists():
        return int(json.loads(mf.read_text(encoding="utf-8")).get("max_length", 512))
    return 512


def model_path() -> str:
    env = os.getenv("RERANKER_MODEL")
    if env:
        return env
    best = ARTIFACT_DIR / "best"
    return str(best) if (best / "config.json").exists() else DEFAULT_MODEL


def threshold() -> float:
    env = os.getenv("RERANKER_THRESHOLD")
    if env:
        return float(env)
    p = Path(model_path())
    mf = p / "manifest.json"
    if mf.exists():
        return float(json.loads(mf.read_text(encoding="utf-8")).get("threshold", 0.5))
    return 0.5


def band() -> tuple[float, float]:
    """2단 판정(GRADE_MODE=two_stage)의 애매 구간 [lo, hi]: lo 이하 no·hi 이상 yes는 로컬에서 확정, 사이만 API 판정.
    RERANKER_LO/RERANKER_HI, 없으면 manifest(band), 그것도 없으면 (0.05, 0.7)."""
    lo, hi = os.getenv("RERANKER_LO"), os.getenv("RERANKER_HI")
    if lo and hi:
        return float(lo), float(hi)
    mf = Path(model_path()) / "manifest.json"
    if mf.exists():
        b = json.loads(mf.read_text(encoding="utf-8")).get("band")
        if b:
            return float(b[0]), float(b[1])
    return 0.05, 0.7


def pair_text(payload: dict) -> str:
    return f"{payload.get('_text', '')}\n최종 원인: {payload.get('root_cause', '')}"


@lru_cache(maxsize=1)
def _load():
    from sentence_transformers import CrossEncoder
    path = model_path()
    ml = _max_length()
    # 기본 cpu: 5쌍 ≈ 0.4s(bge-reranker-base, M1)라 충분하고, MPS는 bge-m3 임베딩과 다른 스레드에서 겹치면 Metal 커맨드 버퍼
    # 어서션으로 프로세스가 죽는다(11-06 eval_graph --workers 3: "failed assertion _status < MTLCommandBufferStatusCommitted").
    m = CrossEncoder(path, max_length=ml, device=os.getenv("RERANKER_DEVICE", "cpu"))
    log.info("reranker loaded: %s (threshold %.3f, max_length %d)", path, threshold(), ml)
    return m


def scores(question: str, texts: list[str]) -> list[float]:
    """관련 확률(sigmoid). 비어 있으면 []."""
    if not texts:
        return []
    with _LOCK:
        out = _load().predict([(question, t) for t in texts], batch_size=16, show_progress_bar=False)
    return [float(x) for x in out]


def grade_cross(question: str, hits: list[Hit]) -> list[bool]:
    thr = threshold()
    return [s >= thr for s in scores(question, [pair_text(h.payload) for h in hits])]


def warmup() -> None:
    scores("워밍업", ["설비: PECVD PECVD-01\n증상: 워밍업\n최종 원인: 없음"])
