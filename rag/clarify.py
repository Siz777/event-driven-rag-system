"""설비 되묻기 (3주차 Step 9). 검색 결과만 보고 결정 — LLM 호출 0회.

왜: 사용자 질의 124건(11-03)의 실패 31건 중 약 18건이 "진공이 떨어진다"·"히터 온도가 안 올라온다"처럼 설비 3종에 같은 증상인
질의였다. 정답 설비를 필터로 강제하면 R@5 0.75 → 0.871(상한). 설비를 물어서 그 상한에 가까이 가는 것이 목표.

조건(should_clarify) — 전부 만족할 때만:
 1. 질의에 알람 코드·설비 ID·설비 키워드가 없다(parse_query 결과가 비어 있음). 코드·설비 질의는 절대 묻지 않는다.
 2. 벡터 top1 cosine ≥ min_cosine. 그 아래는 negative 안전선(grade의 cosine 게이트)이 "이력 없음"을 내야 하므로 묻지 않는다.
 3. 상위 k건의 equipment_type이 2종 이상.
 4. 1·2위의 설비가 다르거나, 같아도 다른 설비의 최상위 티켓 cosine이 1위와 close_gap(0.03) 안 — 1위 설비가 확실히 앞서면 묻지 않는다.
세션당 1회: 되물은 뒤의 답은 질의에 설비가 들어오므로(rewrite_followup) 조건 1에서 자동으로 빠진다.

문구: 후보 설비(상위 k건에 나온 순서, 최대 3개) + 각 설비의 1위 티켓 한 줄. 설비명이나 ID로 답해 달라고 안내.
재검색: 사용자의 답에서 설비 타입을 읽어(parse_query) 원 질의에 "(설비: PECVD)"를 붙인다 → 라우팅 필터가 그 설비로 건다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .query_parse import TYPES, parse_query
from .retrieve import Hit

CLOSE_GAP = 0.03
MAX_CANDIDATES = 3
TYPE_LABEL = {"PECVD": "PECVD(CVD)", "ETCH": "ETCH(식각)", "PVD": "PVD(스퍼터)", "PHOTO": "PHOTO(트랙)", "CMP": "CMP(연마)"}


@dataclass
class Candidate:
    equipment_type: str
    ticket_id: str
    equipment_id: str
    symptom: str


@dataclass
class ClarifyDecision:
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""

    @property
    def types(self) -> list[str]:
        return [c.equipment_type for c in self.candidates]


def _etype(h: Hit) -> str:
    return str((h.payload or {}).get("equipment_type") or "")


def should_clarify(question: str, hits: list[Hit], *, min_cosine: float = 0.0, close_gap: float = CLOSE_GAP) -> ClarifyDecision | None:
    """되물어야 하면 ClarifyDecision(후보 포함), 아니면 None."""
    if len(hits) < 2:
        return None
    f = parse_query(question)
    if f.alarm_codes or f.equipment_ids or f.equipment_types:
        return None
    cs = [h.cosine for h in hits if h.cosine is not None]
    if not cs or max(cs) < min_cosine:
        return None
    types: list[str] = []
    for h in hits:
        t = _etype(h)
        if t and t not in types:
            types.append(t)
    if len(types) < 2:
        return None
    t0 = _etype(hits[0])
    if _etype(hits[1]) != t0:
        reason = "top2_different_types"
    else:
        other = next((h for h in hits if _etype(h) != t0), None)
        c0 = hits[0].cosine
        if other is None or c0 is None or other.cosine is None or c0 - other.cosine >= close_gap:
            return None
        reason = "other_type_close"
    cands: list[Candidate] = []
    for t in types[:MAX_CANDIDATES]:
        h = next(h for h in hits if _etype(h) == t)
        p = h.payload or {}
        cands.append(Candidate(t, h.ticket_id, str(p.get("equipment_id") or ""), (p.get("symptom") or "").replace("\n", " ")[:60]))
    return ClarifyDecision(cands, reason)


def clarify_text(decision: ClarifyDecision) -> str:
    lines = ["어느 설비에서 난 증상인가요? 검색 상위 결과가 여러 설비에 걸쳐 있어 설비를 알면 더 정확히 찾을 수 있습니다."]
    for c in decision.candidates:
        lines.append(f"• {TYPE_LABEL.get(c.equipment_type, c.equipment_type)} — 예: [{c.ticket_id}] {c.equipment_id} · {c.symptom}")
    names = " / ".join(c.equipment_type for c in decision.candidates)
    lines.append(f"이 스레드에 설비명({names})이나 설비 ID(예: {decision.candidates[0].equipment_type}-03)로 답해 주세요.")
    return "\n".join(lines)


def answered_type(text: str) -> str | None:
    """되묻기에 대한 답에서 설비 타입 1개. 코드·ID·키워드 어느 것으로 답해도 된다. 타입이 0개·2개 이상이면 None."""
    f = parse_query(text)
    if len(f.equipment_types) == 1 and f.equipment_types[0] in TYPES:
        return f.equipment_types[0]
    return None


def rewrite_followup(question: str, equipment_type: str) -> str:
    """원 질의 + 설비 → 라우팅 필터가 잡는 형태. parse_query가 키워드로 타입을 읽으므로 되묻기 조건 1에서 빠진다."""
    return f"{question} (설비: {equipment_type})"
