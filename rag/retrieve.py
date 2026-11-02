"""검색. vector(Qdrant, cosine) / bm25(bm25s + Kiwi) / hybrid(RRF).
Hit.score의 의미는 method마다 다르다: vector는 cosine(-1~1), bm25는 BM25 점수(0~, 상한 없음),
hybrid는 RRF 값(두 목록 모두 1위면 2/61 ≈ 0.0328, 한쪽 1위만이면 1/61 ≈ 0.0164).

하이브리드: 벡터 top-20 + BM25 top-20 → RRF(k=60, 가중치 1:1 고정) → top-k.
가중치를 튜닝하지 않는 이유: 평가셋 170행에 맞추면 과적합. 후보 폭(20)은 k의 4배 — 한쪽에서 6~20위인 문서가 합산으로 올라올 여지."""
from dataclasses import dataclass

from . import config, embed
from .index import client


@dataclass
class Hit:
    ticket_id: str
    score: float
    payload: dict


def vector_search(query: str, k: int = config.TOP_K) -> list[Hit]:
    qv = embed.embed([query], kind="query")[0].tolist()
    res = client().query_points(config.COLLECTION, query=qv, limit=k, with_payload=True).points
    return [Hit(p.payload["ticket_id"], float(p.score), p.payload) for p in res]


def bm25_search(query: str, k: int = config.TOP_K) -> list[Hit]:
    from . import bm25_index
    return [Hit(p["ticket_id"], score, p) for score, p in bm25_index.search(query, k)]


RRF_K = 60
CANDIDATES = 20


def rrf(ranked_lists: list[list[Hit]], k: int = config.TOP_K, rrf_k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion. score = Σ 1/(rrf_k + rank). 동점이면 먼저 들어온 목록(벡터) 순서 유지."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    for hits in ranked_lists:
        for rank, h in enumerate(hits, 1):
            scores[h.ticket_id] = scores.get(h.ticket_id, 0.0) + 1.0 / (rrf_k + rank)
            payloads.setdefault(h.ticket_id, h.payload)
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
    return [Hit(tid, sc, payloads[tid]) for tid, sc in top]


def hybrid_search(query: str, k: int = config.TOP_K, candidates: int = CANDIDATES) -> list[Hit]:
    return rrf([vector_search(query, candidates), bm25_search(query, candidates)], k)


def search(query: str, k: int = config.TOP_K, method: str = "vector") -> list[Hit]:
    if method == "vector":
        return vector_search(query, k)
    if method == "bm25":
        return bm25_search(query, k)
    if method == "hybrid":
        return hybrid_search(query, k)
    raise ValueError(f"unknown method: {method}")
