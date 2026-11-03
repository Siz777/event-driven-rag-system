"""노이즈 주입 평가셋 (3주차 Step 7). 규칙 기반, LLM 없음, seed 고정 → 재현 가능.

python -m scripts.make_noise_eval  → domains/manufacturing/eval/eval_noise.jsonl
대상: eval.jsonl의 exact·paraphrase·human 질의(79행). 변형 3종, 행마다 noise 필드:
 (a) typo : 한글 음절 1~2곳의 자모 교체 — 모음 ㅐ↔ㅔ·ㅙ↔ㅚ·ㅒ↔ㅖ, 받침 ㄴ↔ㅁ·ㅇ→ㄴ·ㄹ→ㄴ (밸브→벨브, 펌프다운→펌프다움)
 (b) abbr : 약어·한영 혼용 치환표(긴 것 먼저, 전부 치환) — 챔버→CH, 펌프다운→pump down, 헬륨→He …
 (c) both : abbr 적용 후 남은 한글에 typo
 (d) typo10 / typo30 / both30 : 비율형 — 한글 음절의 10% / 30%를 같은 규칙으로 교체(체크리스트의 "비율 10/30%"). 1~2곳 변형이
     너무 약해 R@5가 안 움직였을 때 어디서 무너지는지 보기 위함.
changed=False인 행(치환 대상 없음)은 깨끗한 질의 그대로 — 집계 때 changed만 세거나 전체를 세거나 둘 다 보고한다.
"""
from __future__ import annotations

import json
import random
import re

from rag import config
from rag.documents import load_jsonl, save_jsonl

TARGET = {"exact", "paraphrase", "human"}
CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = "\0ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
V_SWAP = {"ㅐ": "ㅔ", "ㅔ": "ㅐ", "ㅙ": "ㅚ", "ㅚ": "ㅙ", "ㅒ": "ㅖ", "ㅖ": "ㅒ"}
T_SWAP = {"ㄴ": "ㅁ", "ㅁ": "ㄴ", "ㅇ": "ㄴ", "ㄹ": "ㄴ"}
ABBR = [("베이스 압력", "BP"), ("게이트 밸브", "gate valve"), ("스로틀 밸브", "throttle valve"), ("드라이펌프", "dry pump"),
        ("터보펌프", "TMP"), ("펌프다운", "pump down"), ("챔버", "CH"), ("헬륨", "He"), ("리크", "leak"), ("웨이퍼", "wafer"),
        ("알람", "alarm"), ("냉각수", "coolant"), ("칠러", "chiller"), ("파티클", "particle"), ("유량", "flow"), ("압력", "press."),
        ("온도", "temp"), ("레시피", "recipe"), ("핫플레이트", "hot plate"), ("타겟", "target"), ("슬러리", "slurry"),
        ("포토레지스트", "PR"), ("매처", "matcher"), ("리플렉트", "reflect")]
ABBR.sort(key=lambda kv: -len(kv[0]))


def decompose(ch: str):
    code = ord(ch) - 0xAC00
    return CHO[code // 588], JUNG[(code % 588) // 28], JONG[code % 28]


def compose(c: str, v: str, t: str) -> str:
    return chr(0xAC00 + CHO.index(c) * 588 + JUNG.index(v) * 28 + JONG.index(t))


def typo(text: str, rng: random.Random, rate: float | None = None) -> str:
    """rate=None이면 1~2곳, 아니면 교체 가능한 음절 중 비율만큼(최소 1곳)."""
    chars = list(text)
    cands = []
    for i, ch in enumerate(chars):
        if "가" <= ch <= "힣":
            c, v, t = decompose(ch)
            if v in V_SWAP or (t != "\0" and t in T_SWAP):
                cands.append(i)
    if not cands:                                   # 교체 가능한 자모가 없으면 받침 하나 탈락
        for i, ch in enumerate(chars):
            if "가" <= ch <= "힣" and decompose(ch)[2] != "\0":
                c, v, _ = decompose(ch); chars[i] = compose(c, v, "\0"); return "".join(chars)
        return text
    n = min(len(cands), rng.choice([1, 2])) if rate is None else max(1, round(len(cands) * rate))
    for i in rng.sample(cands, n):
        c, v, t = decompose(chars[i])
        if v in V_SWAP and (t == "\0" or t not in T_SWAP or rng.random() < 0.5):
            v = V_SWAP[v]
        else:
            t = T_SWAP[t]
        chars[i] = compose(c, v, t)
    return "".join(chars)


def abbr(text: str) -> str:
    for k, v in ABBR:
        text = text.replace(k, v)
    return text


def main() -> None:
    rows = [r for r in load_jsonl(config.EVAL_DIR / "eval.jsonl") if r["difficulty"] in TARGET]
    out = []
    for i, r in enumerate(rows):
        q = r["query"]
        variants = {"typo": typo(q, random.Random(1000 + i)), "abbr": abbr(q)}
        variants["both"] = typo(variants["abbr"], random.Random(2000 + i))
        variants["typo10"] = typo(q, random.Random(3000 + i), rate=0.10)
        variants["typo30"] = typo(q, random.Random(4000 + i), rate=0.30)
        variants["both30"] = typo(variants["abbr"], random.Random(5000 + i), rate=0.30)
        for kind, nq in variants.items():
            out.append({**r, "query": nq, "clean_query": q, "noise": kind, "changed": nq != q})
    save_jsonl(config.EVAL_DIR / "eval_noise.jsonl", out)
    stats = {k: sum(1 for o in out if o["noise"] == k and o["changed"]) for k in ("typo", "abbr", "both", "typo10", "typo30", "both30")}
    print(f"rows {len(rows)} × 3 = {len(out)}; changed: {stats}")
    for o in out[:12]:
        if o["changed"] and o["noise"] in ("typo", "typo30", "both30"):
            print(f"[{o['noise']}] {o['clean_query'][:50]}\n   → {o['query'][:50]}")


if __name__ == "__main__":
    main()
