"""Qdrant 적재. 컬렉션은 도메인·임베딩 모델별로 분리."""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import config, embed
from .documents import doc_text, load_tickets

_client: QdrantClient | None = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=config.QDRANT_URL) if config.QDRANT_URL else QdrantClient(path=config.QDRANT_PATH)
    return _client


def build(tickets: list[dict] | None = None) -> int:
    tickets = tickets or load_tickets()
    texts = [doc_text(t) for t in tickets]
    vecs = embed.embed(texts, kind="passage")
    c = client()
    c.recreate_collection(
        config.COLLECTION,
        vectors_config=VectorParams(size=vecs.shape[1], distance=Distance.COSINE),
    )
    points = [
        PointStruct(id=i, vector=vecs[i].tolist(), payload={**t, "_text": texts[i]})
        for i, t in enumerate(tickets)
    ]
    for s in range(0, len(points), 128):
        c.upsert(config.COLLECTION, points[s : s + 128])
    return len(points)


if __name__ == "__main__":
    n = build()
    print(f"indexed {n} tickets into {config.COLLECTION}")
