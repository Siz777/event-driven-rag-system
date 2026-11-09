"""집계형 평가셋(Step 11 Day 2) → eval/eval_aggregate.jsonl. 정답 숫자는 rag/tools.aggregate_tickets(cause_id)로 티켓 데이터에서 계산.

행 구성
- multi_hop 30 (eval.jsonl): "이력 종합·빈도·평균 복구 시간" 질의. expect_tool=True
- human 조치·시간 / 과거 이력 (docs/human_queries.txt 구간, eval.jsonl의 human 행과 질의 텍스트로 매칭): expect_tool은 질의에 집계 신호
  (빈도·평균·최근·사례 수·MTTR·얼마나)가 있을 때만 True — "어떻게 조치했나"는 유사 사례 질문이라 도구가 필요 없다.
- exact 30: 대조군(expect_tool=False) — 도구를 안 불러야 적절.
gold = {count, downtime_mean, downtime_median, last_seen, top_resolution} (정답 원인 cause_id 기준 전체 티켓).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rag import config
from rag.documents import load_jsonl, save_jsonl
from rag.tools import aggregate_tickets

AGG_SIGNAL = re.compile(r"얼마나|빈도|빈번|평균|중앙값|MTTR|복구 ?시간|소요 ?시간|지속시간|가장 최근|최근 조치|몇 ?건|몇 ?번|사례 수|종합|가장 (?:자주|많이)|분포|추세|횟수")


def human_types() -> dict[str, str]:
    out, cur = {}, None
    for line in Path("docs/human_queries.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("# ---"):
            cur = line.replace("# ---", "").strip()
        elif "|" in line and cur:
            out[line.split("|")[0].strip()] = cur
    return out


def main() -> None:
    rows = load_jsonl(config.EVAL_DIR / "eval.jsonl")
    types = human_types()
    out = []
    for r in rows:
        d = r["difficulty"]
        if d == "multi_hop":
            group, expect = "multi_hop", True
        elif d == "human" and types.get(r["query"]) in ("조치·시간", "과거 이력"):
            group, expect = "human_" + ("action_time" if types[r["query"]] == "조치·시간" else "history"), bool(AGG_SIGNAL.search(r["query"]))
        elif d == "exact":
            group, expect = "exact_control", False
        else:
            continue
        a = aggregate_tickets(cause_id=r["cause_id"])
        out.append({**r, "group": group, "expect_tool": expect,
                    "gold": {"count": a["count"], "downtime_mean": a["downtime_min"]["mean"], "downtime_median": a["downtime_min"]["median"],
                             "last_seen": a["last_seen"], "first_seen": a["first_seen"], "top_resolution": a["top_resolution"][0]["resolution"],
                             "top_equipment_id": a["by_equipment_id"][0]["equipment_id"]}})
    save_jsonl(config.EVAL_DIR / "eval_aggregate.jsonl", out)
    from collections import Counter
    print(len(out), Counter(r["group"] for r in out), "expect_tool:", Counter((r["group"], r["expect_tool"]) for r in out))


if __name__ == "__main__":
    main()
