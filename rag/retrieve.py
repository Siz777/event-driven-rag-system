"""검색. vector(Qdrant, cosine) / bm25(bm25s + Kiwi). 3주차 Step 2에서 RRF 하이브리드가 추가된다.
Hit.score의 의미는 method마다 다르다: vector는 cosine(-1~1), bm25는 BM25 점수(0~, 상한 없음)."""
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


def search(query: str, k: int = config.TOP_K, method: str = "vector") -> list[Hit]:
    if method == "vector":
        return vector_search(query, k)
    if method == "bm25":
        return bm25_search(query, k)
    raise ValueError(f"unknown method: {method}")
