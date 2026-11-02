"""BM25용 토크나이저. 도메인 설정(domains/<domain>/tokenizer.yaml)으로 교체한다 — 없으면 한국어(Kiwi).

ko (기본): Kiwi 형태소 분석 + 코드 토큰 보존 + 카탈로그 사용자 사전.
en (github_issues): 소문자 + 코드 토큰(snake_case·버전·하이픈 ID) 선추출 + 영문 불용어 제거 + PyStemmer(Snowball) 어간.
실험 스위치(환경변수): BM25_DICT=0 사용자 사전 끔, BM25_CODES=0 코드 정규식 끔 — Step 7 노이즈 실험에서 기여 분리용.

규칙
- 코드 토큰을 정규식으로 먼저 뽑아 그대로 보존: `E-1101`, `INC-2026-0002` ([A-Z]+-\\d+(-\\d+)*), `PECVD-03`·`CVD07` ([A-Z]{2,}-?\\d+).
  Kiwi는 하이픈에서 쪼개므로(E / - / 1101) 형태소 결과에서 이 조각들은 버리고 코드 원형만 남긴다.
  경계는 `\\b`가 아니라 영숫자 lookaround — `\\b`는 한글을 \\w로 보기 때문에 "PECVD-03에서"에서 코드가 안 잡힌다.
- Kiwi 토큰은 NNG(일반명사)·NNP(고유명사)·SL(외국어)만 유지. 조사·어미·동사·기호 제거.
  단독 SN(숫자: 25, 8, 10 같은 측정값)은 버린다 — 코드 안의 숫자는 정규식 토큰에 이미 들어 있다.
- 모두 소문자화(doc·query 같은 함수를 지나므로 일관됨). Kiwi 분석은 원문 대소문자로 한다(O-ring 사용자 사전 매칭).
- 사용자 사전(NNP)은 catalog.yaml에서 자동 검출한다(`user_words`):
  (a) 설비 유형 이름·vocab·subsystem·root_cause·typical_symptoms·canonical_resolution을 구절 단위로 쪼개고, 각 원문 단어를
      기본 Kiwi로 분석해 조사·어미를 뗀 어간이 2개 이상 형태소로 쪼개지거나 동사·형용사(V*)로 분석되는 것을 통째로 등록
      (펌프다운 → 펌프+다운, 베인 → 베이/VV+ㄴ, 리크 → 리+크/VA, 드라이펌프 → 드라이+펌프).
  (b) 부정 접두어 미·불·비·무 + 명사(미도달, 불균일, 비균일도)는 별도 규칙으로 통째 등록 — Kiwi는 접두어를 XPN으로 떼어내
      "도달"만 남겨 "BP 도달 정상"과 구분이 안 된다.
  (c) 하이픈 포함 단어(O-ring)와 수동 추가(EXTRA_WORDS: 튠 — 카탈로그에는 "튜닝"만 있어 자동 검출이 못 잡는다).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from functools import lru_cache

from . import config
from .documents import load_catalog

USE_DICT = os.getenv("BM25_DICT", "1") != "0"
USE_CODES = os.getenv("BM25_CODES", "1") != "0"


@lru_cache(maxsize=1)
def domain_config() -> dict:
    path = config.DOMAIN_DIR / "tokenizer.yaml"
    if path.exists():
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"lang": "ko"}


KEEP_TAGS = {"NNG", "NNP", "SL"}
# \b는 한글도 \w로 보므로 "PECVD-03에서"에서 경계가 생기지 않는다 → 영숫자 기준 lookaround로 경계를 잡는다
_B0, _B1 = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
CODE_RES = [re.compile(_B0 + r"[A-Z]+-\d+(?:-\d+)*" + _B1),      # E-1101, INC-2026-0002
            re.compile(_B0 + r"[A-Z]{2,}-?\d+" + _B1)]            # PECVD-03, CVD-07, CVD07
_SPLIT = re.compile(r"[()\[\],/·※:;.+→—–]+")
_HAS_KO = re.compile(r"[가-힣]")
_NEG_PREFIX = ("미", "불", "비", "무")
# 어간 뒤에 붙는 문법 형태소: 조사(J*), 어미(E*), 서술격 조사(VCP), 용언 파생 접미사(XSV/XSA), 보조 용언(VX), 기호(S*)
_TRAILING = re.compile(r"^(J|E|VCP|XSV|XSA|VX|S)")
# 자동 검출이 잡지만 사전에 넣으면 해로운 문법어·일반어
_STOP = {"인한", "의한", "따른", "때문", "또는", "및", "등", "후", "시", "중", "내", "약", "경", "거칠"}   # 거칠: 형용사 어간 조각
EXTRA_WORDS = ["튠"]
_PHRASE_FIELDS = ("root_cause", "typical_symptoms", "canonical_resolution", "subsystem")


@lru_cache(maxsize=1)
def _base_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def _phrases(cat: dict) -> list[str]:
    out = []
    for et in cat["equipment_types"].values():
        out.append(et.get("name", ""))
        out += [v for v in et.get("vocab", []) if not v.startswith("※")]
    for c in cat["causes"]:
        for f in _PHRASE_FIELDS:
            v = c.get(f, "")
            out += v if isinstance(v, list) else [v]
    return out


def _word_groups(piece: str) -> list[tuple[str, list]]:
    """구절을 통째로 분석(문맥 반영: "베인 마모"의 베인은 단독 분석과 다르게 쪼개진다)한 뒤 원문 공백 단어별로 형태소를 묶고,
    뒤에 붙은 조사·어미를 뗀 어간 표면형과 어간 형태소 목록을 돌려준다."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", piece)]
    toks = list(_base_kiwi().tokenize(piece))
    out = []
    for ws, we in spans:
        g = [t for t in toks if ws <= t.start < we]
        while g and _TRAILING.match(g[-1].tag):
            g.pop()
        if not g:
            continue
        end = min(g[-1].start + g[-1].len, we)
        out.append((piece[g[0].start:end], g))
    return out


def detect_user_words(catalog: dict | None = None) -> dict[str, list[str]]:
    """{단어: [검출 이유, …]}. 이유: split(2개 이상으로 쪼개짐) / verb(동사·형용사로 분석) / neg_prefix / hyphen / extra."""
    cat = catalog or load_catalog()
    found: dict[str, list[str]] = {}

    def add(w: str, why: str) -> None:
        if w and w not in _STOP and (_HAS_KO.search(w) or "-" in w):
            found.setdefault(w, [])
            if why not in found[w]:
                found[w].append(why)

    for phrase in _phrases(cat):
        for piece in _SPLIT.split(phrase):
            for word in piece.split():
                if "-" in word and _HAS_KO.search(word) is None:
                    add(word, "hyphen")                          # O-ring
            for stem, toks in _word_groups(piece):
                if len(stem) < 2 or not _HAS_KO.search(stem):
                    continue
                if stem.startswith(_NEG_PREFIX) and len(stem) >= 3 and toks[0].tag == "XPN":
                    add(stem, "neg_prefix")
                elif len(toks) >= 2:
                    add(stem, "split")
                elif toks[0].tag.startswith("V"):
                    add(stem, "verb")
    for w in EXTRA_WORDS:
        add(w, "extra")
    return found


def user_words(catalog: dict | None = None) -> list[str]:
    return list(detect_user_words(catalog))


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi
    k = Kiwi()
    if USE_DICT:
        for w in user_words():
            k.add_user_word(w, "NNP", 10.0)
    return k


def _codes(text: str, patterns: list[re.Pattern] | None = None) -> tuple[list[str], str]:
    """코드 토큰 목록과 코드를 공백으로 치환한 나머지 텍스트."""
    if not USE_CODES:
        return [], text
    found = []
    rest = text
    for rx in (patterns or CODE_RES):
        for m in rx.finditer(rest):
            found.append(m.group(0))
        rest = rx.sub(" ", rest)
    return found, rest


# ---- en ----
EN_CODE_RES = [re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9_])"),   # depends_on, COMPOSE_PROJECT_NAME
               re.compile(r"(?<![A-Za-z0-9.])v?\d+\.\d+(?:\.\d+)*(?![A-Za-z0-9.])"),                  # v2.32.0, 1.29
               re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9])")]                  # docker-compose, no-recreate
EN_STOP = set("""a an the and or but if then else of to in on at by for from with without into onto as is are was were be been
being am do does did doing have has had having it its this that these those there here i me my we our you your he she they them
their his her not no nor so than too very can could should would will shall may might must just also only same such which who whom
whose what when where why how all any both each few more most other some own over under up down out off again further once because
while until about above below between through during before after against""".split())


@lru_cache(maxsize=1)
def _stemmer():
    import Stemmer
    return Stemmer.Stemmer(domain_config().get("stem", "english"))


def tokenize_en(text: str) -> list[str]:
    codes, rest = _codes(text, EN_CODE_RES)
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) >= 2 and w not in EN_STOP]
    if domain_config().get("stem"):
        words = _stemmer().stemWords(words)
    return [c.lower() for c in codes] + words


def tokenize(text: str) -> list[str]:
    if domain_config().get("lang") == "en":
        return tokenize_en(text)
    codes, rest = _codes(text)
    toks = [t.form for t in _kiwi().tokenize(rest) if t.tag in KEEP_TAGS]
    return [c.lower() for c in codes] + [t.lower() for t in toks]


def explain(text: str) -> dict:
    """샘플 점검용: 코드 토큰, 형태소(형태/태그/유지 여부), 최종 토큰."""
    if domain_config().get("lang") == "en":
        codes, _ = _codes(text, EN_CODE_RES)
        return {"text": text, "codes": codes, "morphs": [], "tokens": tokenize(text)}
    codes, rest = _codes(text)
    morphs = [(t.form, t.tag, t.tag in KEEP_TAGS) for t in _kiwi().tokenize(rest)]
    return {"text": text, "codes": codes, "morphs": morphs, "tokens": tokenize(text)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", action="store_true", help="사용자 사전 단어 출력(단어\\t이유)")
    ap.add_argument("--json", action="store_true", help="--dict를 JSON으로")
    ap.add_argument("text", nargs="*", help="토큰화 샘플")
    args = ap.parse_args()
    if args.dict:
        d = detect_user_words()
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print(f"# {len(d)} words")
            for w, why in d.items():
                print(f"{w}\t{','.join(why)}")
    for s in args.text:
        e = explain(s)
        dropped = [f"{f}/{g}" for f, g, keep in e["morphs"] if not keep]
        print(f"{s}\n  codes={e['codes']}\n  tokens={e['tokens']}\n  dropped={dropped}")
