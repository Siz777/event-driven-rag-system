"""티켓 로드와 문서 텍스트 변환. 청킹 규칙은 여기 한 곳에서만 정의한다."""
import json
from pathlib import Path

import yaml

from . import config


def load_catalog() -> dict:
    with open(config.DOMAIN_DIR / "catalog.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def save_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_tickets(clean: bool = True) -> list[dict]:
    name = "tickets.clean.jsonl" if clean else "tickets.jsonl"
    path = config.DATA_DIR / name
    if clean and not path.exists():
        path = config.DATA_DIR / "tickets.jsonl"
    return load_jsonl(path)


def doc_text(t: dict, mode: str | None = None) -> str:
    """임베딩·BM25에 들어가는 문서 표현. 티켓 하나 = 청크 하나.
    root_cause를 넣으면 검색이 너무 쉬워지므로 증상·조치 기록만 쓴다.
    mode(기본 config.DOC_TEXT_MODE): full = 설비+증상+조치 기록+조치 결과 / symptom_only = 증상만
    (조치 기록에 부품명이 들어가 검색을 쉽게 만드는지 보는 실험용)."""
    mode = mode or config.DOC_TEXT_MODE
    if mode == "symptom_only":
        return f"증상: {t['symptom']}"
    if mode != "full":
        raise ValueError(f"unknown DOC_TEXT_MODE: {mode}")
    return (
        f"설비: {t['equipment_type']} {t['equipment_id']}\n"
        f"증상: {t['symptom']}\n"
        f"조치 기록: {t['action_log']}\n"
        f"조치 결과: {t['resolution_note']}"
    )
