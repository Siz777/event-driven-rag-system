"""LLM 제공사 비교: 같은 원인·프롬프트·페르소나로 생성해 자동 지표 4개를 표로.
사용: python -m scripts.compare_providers --causes C01,C14,C27,C34
결과: docs/experiments/provider_comparison.md (+ 생성물 jsonl, 사람이 페르소나 구분 채점용)
"""
import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import yaml
from openai import OpenAI

from rag import config, embed
from rag.documents import load_catalog
from rag.llm import parse_json
from scripts.generate_tickets import build_prompt, keywords
from rag.judge import classify


def make_client(p: dict) -> OpenAI:
    return OpenAI(base_url=p["base_url"], api_key=os.getenv(p["api_key_env"], "none"))


def call(client: OpenAI, model: str, prompt: str, temperature: float, retries: int = 3) -> str:
    for i in range(retries):
        try:
            r = client.chat.completions.create(model=model, temperature=temperature,
                                               messages=[{"role": "user", "content": prompt}])
            return r.choices[0].message.content or ""
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--causes", default="C01,C14,C27,C34")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--interval", type=float, default=float(os.getenv("LLM_MIN_INTERVAL", "0")))
    args = ap.parse_args()

    cat = load_catalog()
    causes = {c["cause_id"]: c for c in cat["causes"]}
    want = [causes[c] for c in args.causes.split(",")]
    cfg = yaml.safe_load(open(config.DOMAIN_DIR / "providers.yaml", encoding="utf-8"))
    providers = {p["name"]: p for p in cfg["providers"]}
    judge_desc = f"{config.JUDGE_BASE_URL or 'openai'} / {config.JUDGE_MODEL}"
    tmpl = (config.PROMPT_DIR / "generate_ticket.txt").read_text(encoding="utf-8")
    out_dir = Path("docs/experiments"); out_dir.mkdir(parents=True, exist_ok=True)

    rows, samples = [], []
    for p in cfg["providers"]:
        client = make_client(p)
        n_calls = n_parse_fail = n_items = n_leak = n_pass = 0
        sims, latencies = [], []
        for c in want:
            prompt = build_prompt(tmpl, cat, c, 5, ["2026-03-10"] * 5)
            t0 = time.time()
            try:
                raw = call(client, p["model"], prompt, args.temperature)
                latencies.append(time.time() - t0)
                items = parse_json(raw)
                assert isinstance(items, list) and items
            except Exception as e:
                n_parse_fail += 1; n_calls += 1
                print(f"[{p['name']}] {c['cause_id']} parse/call fail: {type(e).__name__}")
                time.sleep(args.interval); continue
            n_calls += 1
            kws = [k for k in keywords(c["root_cause"]).split(", ") if k]
            texts = []
            for it in items[:5]:
                sym = str(it.get("symptom", ""))
                if not sym:
                    continue
                n_items += 1
                if any(k in sym for k in kws):
                    n_leak += 1
                texts.append(sym)
                # 역분류 (심판 고정)
                opts = [o for o in cat["causes"] if o["equipment_type"] == c["equipment_type"]]
                text = f"증상: {sym}\n조치 기록: {it.get('action_log', '')}"
                pred = classify(text, opts)
                if pred == c["cause_id"] or pred in c.get("confusable_with", []):
                    n_pass += 1
                samples.append({"provider": p["name"], "cause_id": c["cause_id"], "persona": it.get("persona", ""),
                                "symptom": sym, "reverse_pred": pred})
            # 5건 간 평균 쌍별 코사인 (낮을수록 다양)
            if len(texts) >= 2:
                v = embed.embed(texts)
                s = v @ v.T
                iu = np.triu_indices(len(texts), 1)
                sims.append(float(s[iu].mean()))
            time.sleep(args.interval)
        rows.append({
            "provider": p["name"], "model": p["model"], "calls": n_calls,
            "json_fail": f"{n_parse_fail}/{n_calls}",
            "leak_rate": f"{n_leak / max(n_items, 1):.0%}",
            "intra_sim": f"{np.mean(sims):.2f}" if sims else "-",
            "reverse_pass": f"{n_pass / max(n_items, 1):.0%}",
            "latency_s": f"{np.mean(latencies):.1f}" if latencies else "-",
        })

    with open(out_dir / "provider_comparison_samples.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    md = ["# LLM 제공사 비교", "",
          f"원인 {args.causes} × 5페르소나, temperature={args.temperature}, 심판={judge_desc}", "",
          "| 제공사 | 모델 | JSON 실패 | 원인 누수율 ↓ | 5건 간 유사도 ↓ | 역분류 통과율 ↑ | 평균 지연(s) |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['provider']} | {r['model']} | {r['json_fail']} | {r['leak_rate']} | {r['intra_sim']} | {r['reverse_pass']} | {r['latency_s']} |")
    md += ["", "수동 채점(페르소나 구분): `provider_comparison_samples.jsonl`에서 persona를 가리고 읽어 5명 중 누가 썼는지 맞힌 비율을 아래에 기록",
           "", "| 제공사 | 페르소나 구분 (맞힌 수/20) |", "|---|---|"] + [f"| {r['provider']} |  /20 |" for r in rows]
    (out_dir / "provider_comparison.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
