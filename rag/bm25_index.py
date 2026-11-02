"""BM25 색인(bm25s). tickets.clean.jsonl의 doc_text(full)를 rag.tokenize로 토큰화해 저장.

저장 위치: domains/<domain>/artifacts/bm25/ (git 무시, 벡터 .qdrant와 같은 급의 로컬 산출물)
  - index/        bm25s 파라미터·희소 행렬
  - docs.jsonl    색인 순서대로 티켓 payload(+ _text). 검색 결과를 Hit(payload)로 돌려주기 위해
  - manifest.json 버전·문서 수·사전 항목 수·문서 모드·생성 시각
질의 시 같은 tokenize를 쓰고, 점수 0인 자리 채움 결과는 버린다(bm25s는 k개를 항상 채운다).
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import bm25s

from . import config
from .documents import doc_text, load_jsonl, load_tickets, save_jsonl
from .tokenize import USE_CODES, USE_DICT, domain_config, tokenize, user_words

VERSION = 2
# 실험 스위치(BM25_DICT=0 / BM25_CODES=0)는 별도 디렉터리에 색인해 기본 색인을 덮지 않는다
_SUFFIX = ("" if USE_DICT else "_nodict") + ("" if USE_CODES else "_nocode")
BM25_DIR = config.DOMAIN_DIR / "artifacts" / f"bm25{_SUFFIX}"


def build(tickets: list[dict] | None = None, out_dir: Path = BM25_DIR) -> dict:
    tickets = tickets or load_tickets()
    texts = [doc_text(t) for t in tickets]
    corpus = [tokenize(x) for x in texts]
    retriever = bm25s.BM25()
    retriever.index(corpus, show_progress=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    retriever.save(str(out_dir / "index"))
    save_jsonl(out_dir / "docs.jsonl", [{**t, "_text": texts[i]} for i, t in enumerate(tickets)])
    manifest = {"version": VERSION, "bm25s": bm25s.__version__, "n_docs": len(tickets),
                "tokenizer": domain_config(), "use_dict": USE_DICT, "use_codes": USE_CODES,
                "n_user_words": len(user_words()) if USE_DICT else 0, "doc_text_mode": config.DOC_TEXT_MODE,
                "avg_tokens": round(sum(map(len, corpus)) / max(len(corpus), 1), 1),
                "vocab_size": len({tok for c in corpus for tok in c}),
                "at": datetime.now().isoformat(timespec="minutes")}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _load.cache_clear()
    return manifest


@lru_cache(maxsize=1)
def _load(out_dir: str = str(BM25_DIR)) -> tuple[bm25s.BM25, list[dict]]:
    d = Path(out_dir)
    if not (d / "manifest.json").exists():
        raise FileNotFoundError(f"BM25 index missing at {d}: run `python -m rag.bm25_index`")
    return bm25s.BM25.load(str(d / "index")), load_jsonl(d / "docs.jsonl")


def n_docs() -> int:
    return len(_load()[1])


def search(query: str, k: int = config.TOP_K) -> list[tuple[float, dict]]:
    """[(score, payload)] 점수 내림차순, 점수 0 제외."""
    retriever, docs = _load()
    toks = tokenize(query)
    if not toks:
        return []
    res = retriever.retrieve([toks], k=min(k, len(docs)), show_progress=False)
    out = []
    for idx, score in zip(res.documents[0], res.scores[0]):
        if score <= 0:
            break
        out.append((float(score), docs[int(idx)]))
    return out


if __name__ == "__main__":
    m = build()
    print(json.dumps(m, ensure_ascii=False, indent=2))
