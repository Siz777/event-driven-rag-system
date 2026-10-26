"""임베딩. 학습/서빙/평가가 전부 이 함수 하나를 쓴다 (불일치 방지)."""
import numpy as np
from sentence_transformers import SentenceTransformer

from . import config

_model: SentenceTransformer | None = None


def model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(config.EMBED_MODEL)
    return _model


def _prefix(texts: list[str], kind: str) -> list[str]:
    # e5 계열은 query:/passage: 접두어가 없으면 성능이 크게 떨어진다
    if "e5" in config.EMBED_MODEL.lower():
        p = "query: " if kind == "query" else "passage: "
        return [p + t for t in texts]
    return texts


def embed(texts: list[str], kind: str = "passage") -> np.ndarray:
    vecs = model().encode(
        _prefix(texts, kind), batch_size=config.EMBED_BATCH,
        normalize_embeddings=True, show_progress_bar=len(texts) > 64,
    )
    return np.asarray(vecs, dtype=np.float32)


def dim() -> int:
    return model().get_sentence_embedding_dimension()
