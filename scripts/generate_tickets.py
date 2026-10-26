"""합성 티켓 생성. 원인은 카탈로그에서 복사, 표현만 LLM.
사용: python -m scripts.generate_tickets --per-cause 8
중단돼도 같은 명령으로 재개된다 (배치마다 저장, 원인별 부족분만 생성).
"""
import argparse
import json
import random
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from tqdm import tqdm

from rag import config
from rag.documents import load_catalog, load_jsonl
from rag.llm import chat_json

PERSONAS = [
    "1. 작성자: 신입 오퍼레이터 / 문체: 짧고 비전문적, 보이는 증상 위주, 원인 추측 없음, 존댓말",
    "2. 작성자: 10년차 설비 엔지니어 / 문체: 전문 용어와 약어(CH, BP, TMP, MFC 등), 측정 수치와 로그 포함, 개조식",
    "3. 작성자: 야간 근무자 / 문체: 급하게 쓴 메모, 오타 1~2개, 문장 미완성, 시간 표기 포함",
    "4. 작성자: 협력사 기술자 / 문체: 한영 혼용, 부품은 영문명(valve, pump, chamber 등), 격식체",
    "5. 작성자: 품질팀 담당자 / 문체: 격식체, 영향 범위(공정 중단 시간, 웨이퍼 손실 수량, 로트 홀드) 강조",
]
STOP = {"인한", "으로", "및", "종료", "이상", "초과", "실패", "저하", "열화", "불량", "오차", "지연", "편차",
        "오염", "고장", "마모", "손상", "누설", "막힘", "고착", "단선", "드리프트", "이탈", "불균일", "심화", "응답"}


def keywords(root_cause: str) -> str:
    """원인 문장의 핵심 명사 → symptom에 '쓰지 말 것' 목록."""
    toks = re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)?|[가-힣]{2,}", root_cause)
    toks = [re.sub(r"(으로|로|의|에)$", "", t) for t in toks]   # 조사 제거
    toks = [t for t in toks if len(t) >= 2]
    return ", ".join(dict.fromkeys(t for t in toks if t not in STOP)) or root_cause


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-cause", type=int, default=8)
    ap.add_argument("--out", default=str(config.DATA_DIR / "tickets.jsonl"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--only", default="", help="쉼표로 cause_id 지정 시 그 원인만 생성 (부족분 보충용)")
    args = ap.parse_args()
    random.seed(args.seed)

    cat = load_catalog()
    causes = cat["causes"]
    if args.only:
        want = set(args.only.split(","))
        causes = [c for c in causes if c["cause_id"] in want]
    tmpl = (config.PROMPT_DIR / "generate_ticket.txt").read_text(encoding="utf-8")
    year = date.today().year

    # 체크포인트: 기존 파일을 읽어 원인별 완료 수와 다음 ticket 번호를 복원 → 같은 명령으로 재개 가능
    out = Path(args.out)
    existing = load_jsonl(out) if out.exists() else []
    done = Counter(t["cause_id"] for t in existing)
    seq = 1 + max((int(t["ticket_id"].rsplit("-", 1)[1]) for t in existing), default=0)
    if existing:
        print(f"[resume] 기존 {len(existing)}건, 다음 번호 {seq}")
    out.parent.mkdir(parents=True, exist_ok=True)
    total = len(existing)

    for c in tqdm(causes, desc="causes"):
        et = cat["equipment_types"][c["equipment_type"]]
        made, attempts = done.get(c["cause_id"], 0), 0
        while made < args.per_cause and attempts < 6:
            attempts += 1
            n = min(5, args.per_cause - made)
            prompt = tmpl.format(
                n=n, equipment_name=et["name"], equipment_type=c["equipment_type"],
                id_min=et["id_range"][0], id_max=et["id_range"][1], subsystem=c["subsystem"],
                root_cause=c["root_cause"], typical_symptoms=", ".join(c["typical_symptoms"]),
                personas="\n".join(PERSONAS[:n]), root_cause_keywords=keywords(c["root_cause"]),
                alarm_code=c["alarm_code"],
            )
            try:
                items = chat_json(prompt, temperature=args.temperature)
            except Exception as e:
                print(f"[retry] {c['cause_id']}: {e}")
                continue
            batch = []
            for it in items[:n]:
                if not it.get("symptom"):
                    continue
                d = date(year, 1, 1) + timedelta(days=random.randint(0, 300))
                batch.append({
                    "ticket_id": f"INC-{year}-{seq:04d}",
                    "cause_id": c["cause_id"],
                    "equipment_type": c["equipment_type"],
                    "equipment_id": it.get("equipment_id") or f"{c['equipment_type']}-{random.randint(*et['id_range']):02d}",
                    "subsystem": c["subsystem"],
                    "alarm_code": c["alarm_code"],
                    "symptom": it["symptom"],
                    "action_log": it.get("action_log", ""),
                    "resolution_note": it.get("resolution_note", ""),
                    "root_cause": c["root_cause"],                 # 카탈로그에서 복사 — LLM이 쓰지 않음
                    "canonical_resolution": c["canonical_resolution"],
                    "downtime_min": int(it.get("downtime_min") or random.randint(30, 480)),
                    "date": d.isoformat(),
                    "persona": str(it.get("persona", "")),
                })
                seq += 1
                made += 1
            with open(out, "a", encoding="utf-8") as f:       # 배치마다 즉시 저장
                for r in batch:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(batch)
    print(f"total {total} tickets -> {out}")


if __name__ == "__main__":
    main()
