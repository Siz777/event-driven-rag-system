"""사람이 쓴 질의를 eval.jsonl에 추가한다 (LLM 생성 질의끼리 매칭되는 편향 완화).
입력 텍스트 파일: 한 줄 = 질의 + 구분자 + cause_id.  구분자는 탭, '|', ','.  예)
    펌프다운이 평소보다 오래 걸리는데 예전에 비슷한 일 있었어?	C01
    척이 안 잠기고 백사이드 헬륨 수치가 올라가 | C14
    설비와 무관한 질문이면 cause_id 자리에 NEG  (negative로 들어감)
빈 줄과 #으로 시작하는 줄은 무시. 줄 끝에 ' #human'이 있으면 source=human(사람이 쓴/고친 질의), 없으면 source=assistant_draft.
사용: python -m scripts.append_human_queries docs/human_queries.txt
"""
import argparse
import re
from collections import defaultdict

from rag import config
from rag.documents import load_jsonl, load_tickets, save_jsonl

LINE = re.compile(r"^(?P<q>.+?)\s*[\t|,]\s*(?P<c>C\d{2}|NEG)\s*(?P<h>#human)?\s*$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--eval", default=str(config.EVAL_DIR / "eval.jsonl"))
    args = ap.parse_args()

    by_cause = defaultdict(list)
    for t in load_tickets():
        by_cause[t["cause_id"]].append(t["ticket_id"])

    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl") if (config.EVAL_DIR / "eval.jsonl").exists() else []
    existing = {r["query"] for r in rows}
    added, skipped = 0, []
    for ln, line in enumerate(open(args.path, encoding="utf-8"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            skipped.append(f"{ln}: 형식 불일치 — {line[:40]}"); continue
        q, c = m["q"].strip(), m["c"]
        source = "human" if m["h"] else "assistant_draft"
        if q in existing:
            skipped.append(f"{ln}: 중복 — {q[:40]}"); continue
        if c != "NEG" and not by_cause.get(c):
            skipped.append(f"{ln}: 티켓 없는 cause_id {c}"); continue
        rows.append({"query": q, "difficulty": "negative" if c == "NEG" else "human", "source_ticket_id": "",
                     "relevant_ticket_ids": [] if c == "NEG" else by_cause[c], "cause_id": "" if c == "NEG" else c,
                     "source": source})
        existing.add(q); added += 1
    save_jsonl(config.EVAL_DIR / "eval.jsonl", rows)
    print(f"added {added} human queries -> {len(rows)} rows total")
    for s in skipped:
        print("  skip", s)


if __name__ == "__main__":
    main()
