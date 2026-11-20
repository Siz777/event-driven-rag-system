"""임베딩. 학습/서빙/평가가 전부 이 함수 하나를 쓴다 (불일치 방지)."""
import numpy as np
from sentence_transformers import SentenceTransformer

from . import config

_model: SentenceTransformer | None = None


def model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(config.EMBED_MODEL, device=config.EMBED_DEVICE)
    return _model


# Qwen3-Embedding은 질의에만 instruction을 붙인다(문서는 그대로). Step 12에서 쓴 문구 — 바꾸면 색인은 그대로여도 숫자가 달라진다
QWEN3_QUERY_INSTRUCTION = "Instruct: Given a query, retrieve relevant incident tickets\nQuery: "


def query_prompt(model_name: str | None = None) -> str:
    """질의 앞에 붙는 문자열(없으면 ""). 평가 행(experiments.jsonl)에 그대로 기록한다."""
    name = (model_name or config.EMBED_MODEL).lower()
    if "qwen3-embedding" in name:
        return QWEN3_QUERY_INSTRUCTION
    if "e5" in name:
        return "query: "
    return ""


def _prefix(texts: list[str], kind: str) -> list[str]:
    if kind == "query":
        p = query_prompt()
    else:
        # e5 계열은 query:/passage: 접두어가 없으면 성능이 크게 떨어진다
        p = "passage: " if "e5" in config.EMBED_MODEL.lower() else ""
    return [p + t for t in texts] if p else texts


def embed(texts: list[str], kind: str = "passage") -> np.ndarray:
    vecs = model().encode(
        _prefix(texts, kind), batch_size=config.EMBED_BATCH,
        normalize_embeddings=True, show_progress_bar=len(texts) > 64,
    )
    return np.asarray(vecs, dtype=np.float32)


def dim() -> int:
    return model().get_sentence_embedding_dimension()
