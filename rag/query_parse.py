"""질의에서 메타데이터 필터 추출 (사전 매칭, LLM 아님).

- 알람 코드: E-1101 / e1101 / E1101 / e-1101 → "E-1101" (4자리 숫자). 경계는 영숫자 lookaround(한글은 \\w).
- 설비 ID: PECVD-03 / pecvd03 / CVD-07 → "PECVD-03" (타입 + 2자리). CVD는 PECVD의 별칭.
- 설비 타입 키워드: PECVD/CVD, ETCH/에처/식각/에칭, PVD/스퍼터, PHOTO/포토/트랙/노광/스캐너, CMP/연마.
  설비 ID가 있으면 그 타입도 포함. 타입이 2개 이상이면 비교 질문으로 보고 타입 필터를 걸지 않는다(ambiguous).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_B0, _B1 = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
TYPES = ["PECVD", "ETCH", "PVD", "PHOTO", "CMP"]
ALIASES = {"CVD": "PECVD"}
KEYWORDS: dict[str, list[str]] = {
    "PECVD": ["pecvd", "cvd"],
    "ETCH": ["etch", "에처", "식각", "에칭", "etcher"],
    "PVD": ["pvd", "스퍼터", "sputter"],
    "PHOTO": ["photo", "포토", "트랙", "노광", "스캐너", "scanner"],
    "CMP": ["cmp", "연마"],
}
CODE_RE = re.compile(_B0 + r"[Ee]-?(\d{4})" + _B1)
ID_RE = re.compile(_B0 + r"(PECVD|ETCH|PVD|PHOTO|CMP|CVD)[- ]?(\d{1,2})" + _B1, re.IGNORECASE)
_KW_RES = {t: [re.compile(_B0 + re.escape(k) + _B1 if k.isascii() else re.escape(k), re.IGNORECASE) for k in ks]
           for t, ks in KEYWORDS.items()}


@dataclass
class QueryFilters:
    alarm_codes: list[str] = field(default_factory=list)
    equipment_ids: list[str] = field(default_factory=list)
    equipment_types: list[str] = field(default_factory=list)   # 길이 1일 때만 필터로 쓴다
    ambiguous_types: bool = False                               # 타입 2개 이상 → 필터 안 함

    @property
    def type_filter(self) -> list[str]:
        return self.equipment_types if len(self.equipment_types) == 1 else []

    def is_empty(self) -> bool:
        return not self.alarm_codes and not self.type_filter

    def as_dict(self) -> dict:
        return {"alarm_codes": self.alarm_codes, "equipment_ids": self.equipment_ids,
                "equipment_types": self.equipment_types, "ambiguous_types": self.ambiguous_types}


def parse_query(text: str) -> QueryFilters:
    f = QueryFilters()
    rest = text
    for m in CODE_RE.finditer(text):
        code = f"E-{m.group(1)}"
        if code not in f.alarm_codes:
            f.alarm_codes.append(code)
    rest = CODE_RE.sub(" ", rest)
    types: list[str] = []
    for m in ID_RE.finditer(rest):
        t = ALIASES.get(m.group(1).upper(), m.group(1).upper())
        eid = f"{t}-{int(m.group(2)):02d}"
        if eid not in f.equipment_ids:
            f.equipment_ids.append(eid)
        if t not in types:
            types.append(t)
    rest = ID_RE.sub(" ", rest)
    for t, rxs in _KW_RES.items():
        if t not in types and any(rx.search(rest) for rx in rxs):
            types.append(t)
    f.equipment_types = types
    f.ambiguous_types = len(types) > 1
    return f
