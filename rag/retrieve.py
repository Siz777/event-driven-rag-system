"""검색. 1주차는 벡터만. 3주차에 BM25 + RRF가 여기 추가된다."""
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


def search(query: str, k: int = config.TOP_K, method: str = "vector") -> list[Hit]:
    if method == "vector":
        return vector_search(query, k)
    raise ValueError(f"unknown method: {method}")
