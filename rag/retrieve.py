"""검색. vector(Qdrant, cosine) / bm25(bm25s + Kiwi) / hybrid(RRF).
Hit.score의 의미는 method마다 다르다: vector는 cosine(-1~1), bm25는 BM25 점수(0~, 상한 없음),
hybrid는 RRF 값(두 목록 모두 1위면 2/61 ≈ 0.0328, 한쪽 1위만이면 1/61 ≈ 0.0164).

하이브리드: 벡터 top-20 + BM25 top-20 → RRF(k=60, 가중치 1:1 고정) → top-k.
가중치를 튜닝하지 않는 이유: 평가셋 170행에 맞추면 과적합. 후보 폭(20)은 k의 4배 — 한쪽에서 6~20위인 문서가 합산으로 올라올 여지."""
from dataclasses import dataclass

from qdrant_client.models import FieldCondition, Filter, MatchAny

from . import config, embed
from .index import client
from .query_parse import QueryFilters, parse_query


@dataclass
class Hit:
    ticket_id: str
    score: float            # method별 점수(vector cosine / BM25 / RRF)
    payload: dict
    cosine: float | None = None   # 벡터 경로를 거친 문서의 cosine. BM25 단독 결과는 None. grade·인용 점수용


def _qdrant_filter(alarm_codes: list[str] | None, equipment_types: list[str] | None) -> Filter | None:
    must = []
    if alarm_codes:
        must.append(FieldCondition(key="alarm_code", match=MatchAny(any=list(alarm_codes))))
    if equipment_types:
        must.append(FieldCondition(key="equipment_type", match=MatchAny(any=list(equipment_types))))
    return Filter(must=must) if must else None


def vector_search(query: str, k: int = config.TOP_K, *, alarm_codes: list[str] | None = None,
                  equipment_types: list[str] | None = None) -> list[Hit]:
    qv = embed.embed([query], kind="query")[0].tolist()
    res = client().query_points(config.COLLECTION, query=qv, limit=k, with_payload=True,
                                query_filter=_qdrant_filter(alarm_codes, equipment_types)).points
    return [Hit(p.payload["ticket_id"], float(p.score), p.payload, cosine=float(p.score)) for p in res]


def _accept(p: dict, alarm_codes: list[str] | None, equipment_types: list[str] | None) -> bool:
    return (not alarm_codes or p.get("alarm_code") in alarm_codes) and \
           (not equipment_types or p.get("equipment_type") in equipment_types)


def bm25_search(query: str, k: int = config.TOP_K, *, alarm_codes: list[str] | None = None,
                equipment_types: list[str] | None = None) -> list[Hit]:
    """필터가 있으면 전체 점수를 받아 사후 필터(문서 318건이라 비용 무시)."""
    from . import bm25_index
    if not alarm_codes and not equipment_types:
        return [Hit(p["ticket_id"], score, p) for score, p in bm25_index.search(query, k)]
    out = [Hit(p["ticket_id"], score, p) for score, p in bm25_index.search(query, bm25_index.n_docs())
           if _accept(p, alarm_codes, equipment_types)]
    return out[:k]


RRF_K = 60
CANDIDATES = 20


def rrf(ranked_lists: list[list[Hit]], k: int = config.TOP_K, rrf_k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion. score = Σ 1/(rrf_k + rank). 동점이면 먼저 들어온 목록(벡터) 순서 유지."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    cosines: dict[str, float] = {}
    for hits in ranked_lists:
        for rank, h in enumerate(hits, 1):
            scores[h.ticket_id] = scores.get(h.ticket_id, 0.0) + 1.0 / (rrf_k + rank)
            payloads.setdefault(h.ticket_id, h.payload)
            if h.cosine is not None:
                cosines[h.ticket_id] = h.cosine
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
    return [Hit(tid, sc, payloads[tid], cosine=cosines.get(tid)) for tid, sc in top]


def hybrid_search(query: str, k: int = config.TOP_K, candidates: int = CANDIDATES,
                  equipment_types: list[str] | None = None, trace: dict | None = None) -> list[Hit]:
    vec = vector_search(query, candidates, equipment_types=equipment_types)
    if trace is not None:
        trace["vector_top1"] = vec[0].cosine if vec else None
    return rrf([vec, bm25_search(query, candidates, equipment_types=equipment_types)], k)


def filtered_search(query: str, k: int = config.TOP_K, candidates: int = CANDIDATES,
                    filters: QueryFilters | None = None, trace: dict | None = None) -> list[Hit]:
    """메타데이터 라우팅 (Step 3).
    - 알람 코드가 있으면 alarm_code 일치로 후보를 제한하고 **BM25 순위만** 쓴다(벡터 혼합 안 함 — 코드 질의에서 벡터는 잡음).
      코드로 걸리는 문서가 없으면(미지 코드) 코드 필터를 버리고 아래로 폴백.
    - 설비 타입이 하나 잡히면 equipment_type으로 제한한 hybrid(RRF).
    - 둘 다 없으면 보통 hybrid.
    trace(dict)를 주면 실제 적용된 필터와 분기를 기록한다(평가 집계용)."""
    f = filters or parse_query(query)
    applied = "none"
    hits: list[Hit] = []
    if f.alarm_codes:
        hits = bm25_search(query, k, alarm_codes=f.alarm_codes, equipment_types=f.type_filter or None)
        if not hits and f.type_filter:                       # 코드+타입이 서로 어긋남 → 코드만
            hits = bm25_search(query, k, alarm_codes=f.alarm_codes)
        applied = "code" if hits else "code_fallback"
    if not hits and f.type_filter:
        hits = hybrid_search(query, k, candidates, equipment_types=f.type_filter, trace=trace)
        applied = "type" if applied == "none" else applied + "+type"
    if not hits:
        hits = hybrid_search(query, k, candidates, trace=trace)
        if applied != "none":
            applied += "+none"
    if trace is not None:
        trace.update({"filters": f.as_dict(), "applied": applied})
        trace.setdefault("vector_top1", None)             # 코드 분기(BM25만)는 cosine 없음
    return hits


def search(query: str, k: int = config.TOP_K, method: str = "vector", trace: dict | None = None) -> list[Hit]:
    if method == "vector":
        return vector_search(query, k)
    if method == "bm25":
        return bm25_search(query, k)
    if method == "hybrid":
        return hybrid_search(query, k)
    if method == "hybrid+filter":
        return filtered_search(query, k, trace=trace)
    raise ValueError(f"unknown method: {method}")
