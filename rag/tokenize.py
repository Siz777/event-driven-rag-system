"""BM25용 한국어 토크나이저 (Kiwi 형태소 분석 + 코드 토큰 보존 + 카탈로그 사용자 사전).

규칙
- 코드 토큰을 정규식으로 먼저 뽑아 그대로 보존: `E-1101`, `INC-2026-0002` ([A-Z]+-\\d+(-\\d+)*), `PECVD-03`·`CVD07` ([A-Z]{2,}-?\\d+).
  Kiwi는 하이픈에서 쪼개므로(E / - / 1101) 형태소 결과에서 이 조각들은 버리고 코드 원형만 남긴다.
- Kiwi 토큰은 NNG(일반명사)·NNP(고유명사)·SL(외국어)·SN(숫자) 태그만 유지. 조사·어미·동사·기호 제거.
- 모두 소문자화(doc·query 같은 함수를 지나므로 일관됨). Kiwi 분석은 원문 대소문자로 한다(O-ring 사용자 사전 매칭).
- 사용자 사전: catalog.yaml에서 자동 추출(설비 유형 이름·vocab 항목·subsystem·root_cause 핵심 명사) → NNP.
  "베인"이 동사(베이+ㄴ)로, "O-ring"이 O/-/ring으로 쪼개지는 것을 막는다. 띄어쓰기 없는 단어만 등록(BM25는 단일 토큰이 유리).
"""
from __future__ import annotations

import argparse
import re
from functools import lru_cache

from .documents import load_catalog

KEEP_TAGS = {"NNG", "NNP", "SL", "SN"}
# \b는 한글도 \w로 보므로 "PECVD-03에서"에서 경계가 생기지 않는다 → 영숫자 기준 lookaround로 경계를 잡는다
_B0, _B1 = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
CODE_RES = [re.compile(_B0 + r"[A-Z]+-\d+(?:-\d+)*" + _B1),      # E-1101, INC-2026-0002
            re.compile(_B0 + r"[A-Z]{2,}-?\d+" + _B1)]            # PECVD-03, CVD-07, CVD07
_SPLIT = re.compile(r"[()\[\],/·※:;]+")
_PARTICLE = re.compile(r"(으로|로|의|에서|에|과|와)$")
_STOP = {"인한", "및", "따른", "때문", "또는", "의한", "관련", "없음", "등"}
_HAS_KO = re.compile(r"[가-힣]")


def _words_from_phrase(phrase: str, strip_particles: bool) -> list[str]:
    out = []
    for piece in _SPLIT.split(phrase):
        for w in piece.split():
            w = w.strip()
            if strip_particles:
                w2 = _PARTICLE.sub("", w)
                w = w2 if len(w2) >= 2 else w
            if len(w) < 2 or w in _STOP:
                continue
            if _HAS_KO.search(w) or "-" in w:            # 순수 영문 단어는 SL로 어차피 보존
                out.append(w)
    return out


def user_words(catalog: dict | None = None) -> list[str]:
    """카탈로그에서 NNP로 등록할 단어 목록(중복 제거, 등장 순서 유지)."""
    cat = catalog or load_catalog()
    seen: dict[str, None] = {}
    for et in cat["equipment_types"].values():
        for w in _words_from_phrase(et.get("name", ""), False):
            seen.setdefault(w)
        for v in et.get("vocab", []):
            if v.startswith("※"):
                continue
            for w in _words_from_phrase(v, False):
                seen.setdefault(w)
    for c in cat["causes"]:
        for w in _words_from_phrase(c.get("subsystem", ""), False):
            seen.setdefault(w)
        for w in _words_from_phrase(c.get("root_cause", ""), True):
            seen.setdefault(w)
    return list(seen)


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi
    k = Kiwi()
    for w in user_words():
        k.add_user_word(w, "NNP", 10.0)
    return k


def _codes(text: str) -> tuple[list[str], str]:
    """코드 토큰 목록과 코드를 공백으로 치환한 나머지 텍스트."""
    found = []
    rest = text
    for rx in CODE_RES:
        for m in rx.finditer(rest):
            found.append(m.group(0))
        rest = rx.sub(" ", rest)
    return found, rest


def tokenize(text: str) -> list[str]:
    codes, rest = _codes(text)
    toks = [t.form for t in _kiwi().tokenize(rest) if t.tag in KEEP_TAGS]
    return [c.lower() for c in codes] + [t.lower() for t in toks]


def explain(text: str) -> dict:
    """샘플 점검용: 코드 토큰, 형태소(형태/태그/유지 여부), 최종 토큰."""
    codes, rest = _codes(text)
    morphs = [(t.form, t.tag, t.tag in KEEP_TAGS) for t in _kiwi().tokenize(rest)]
    return {"text": text, "codes": codes, "morphs": morphs, "tokens": tokenize(text)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", action="store_true", help="사용자 사전 단어 출력")
    ap.add_argument("text", nargs="*", help="토큰화 샘플")
    args = ap.parse_args()
    if args.dict:
        ws = user_words()
        print(f"# {len(ws)} words")
        print("\n".join(ws))
    for s in args.text:
        e = explain(s)
        dropped = [f"{f}/{g}" for f, g, keep in e["morphs"] if not keep]
        print(f"{s}\n  codes={e['codes']}\n  tokens={e['tokens']}\n  dropped={dropped}")
