"""GitHub 이슈 교차 검증용 수집 (3주차 Step 6). gh CLI 인증을 쓴다(`gh auth status`).

python -m scripts.collect_github_issues --repo docker/compose --bug-label kind/bug --corpus 2000

산출물 (domains/github_issues/)
- data/tickets.jsonl : 코퍼스. {ticket_id: "GH-<n>", number, title, body(정제), created_at, labels, url}
- eval/eval.jsonl    : 중복 쌍. {query(나중 이슈 제목 + 본문 500자, 누수 제거), relevant_ticket_ids: [원본], source_ticket_id,
                       difficulty: "duplicate", source: "github", created_at(질의 이슈), dup_number}
- data/collect_report.json : 건수·제외 사유

규칙
- 중복 이슈 = label:duplicate 또는 state_reason=duplicate. 원본은 (1) 코멘트의 "duplicate of #N / dup of #N / duplicates #N",
  (2) 없으면 본문의 같은 패턴. 원본 번호가 중복 이슈보다 작아야(먼저 생성) 한다. 여러 개면 첫 번째.
- 코퍼스 = bug 라벨 이슈 최신순 N건(PR 제외, 봇 제외). 중복 쌍의 원본이 코퍼스에 없으면 원본을 코퍼스에 추가한다(bug 라벨이
  없어도 — 쌍을 살리기 위해, 추가 건수는 report에). 중복 이슈 자체는 코퍼스에서 뺀다(질의이므로).
- 누수 차단: 질의·본문에서 "#숫자", 이슈 URL, "duplicate/dup" 단어를 지운다. 템플릿 헤딩(### …)·HTML 주석 제거.
  정제 후 본문 100자 미만이면 템플릿만 남은 이슈로 보고 제외.
- 시간 누수(질의보다 나중 이슈가 코퍼스에 있음)는 평가 시 created_at으로 거른다(scripts/eval_github.py).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "domains" / "github_issues"
DUP_RE = re.compile(r"(?:duplicate of|duplicates|dup of|dupe of|duplicate:)\s*(?:#|https?://github\.com/[\w.-]+/[\w.-]+/issues/)(\d+)", re.I)
ISSUE_REF = re.compile(r"(?:https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+|#\d+)")
DUP_WORD = re.compile(r"\b(duplicates?|dupes?|dup)\b", re.I)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HEADING = re.compile(r"^#{1,6}\s.*$", re.M)
CODE_FENCE = re.compile(r"```.*?```", re.S)


def gh(path: str, *params: str, paginate: bool = False) -> list | dict:
    cmd = ["gh", "api", "-X", "GET", path] + [x for p in params for x in ("-f", p)]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    for attempt in range(4):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            if paginate:                      # --slurp: 페이지 배열. search는 {items}, list는 [..]
                flat = []
                for page in data:
                    flat += page["items"] if isinstance(page, dict) else page
                return flat
            return data
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gh api failed: {' '.join(cmd)}\n{r.stderr[:300]}")


def clean(text: str | None) -> str:
    t = text or ""
    t = HTML_COMMENT.sub(" ", t)
    t = HEADING.sub(" ", t)
    t = ISSUE_REF.sub(" ", t)
    t = DUP_WORD.sub(" ", t)
    t = re.sub(r"\r", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def is_bot(issue: dict) -> bool:
    u = issue.get("user") or {}
    return u.get("type") == "Bot" or str(u.get("login", "")).endswith("[bot]")


def to_doc(issue: dict) -> dict:
    return {"ticket_id": f"GH-{issue['number']}", "number": issue["number"], "title": issue["title"],
            "body": clean(issue.get("body"))[:3000], "created_at": issue["created_at"],
            "labels": [l["name"] for l in issue.get("labels", [])], "url": issue["html_url"]}


def find_original(repo: str, dup: dict) -> int | None:
    comments = gh(f"repos/{repo}/issues/{dup['number']}/comments", "per_page=100")
    texts = [c.get("body") or "" for c in comments] + [dup.get("body") or ""]
    for t in texts:
        for m in DUP_RE.finditer(t):
            n = int(m.group(1))
            if n < dup["number"]:
                return n
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="docker/compose")
    ap.add_argument("--bug-label", default="kind/bug")
    ap.add_argument("--corpus", type=int, default=2000)
    args = ap.parse_args()
    report: dict = {"repo": args.repo, "bug_label": args.bug_label}

    dups = gh("search/issues", f"q=repo:{args.repo} is:issue label:duplicate", "per_page=100", paginate=True)
    dups += gh("search/issues", f"q=repo:{args.repo} is:issue reason:duplicate", "per_page=100", paginate=True)
    seen = {}
    for d in dups:
        seen.setdefault(d["number"], d)
    dups = sorted(seen.values(), key=lambda d: d["number"])
    report["duplicate_issues"] = len(dups)
    print(f"duplicate-marked issues: {len(dups)}")

    pairs = []
    no_orig = 0
    for i, d in enumerate(dups, 1):
        if is_bot(d):
            continue
        orig = find_original(args.repo, d)
        if orig is None:
            no_orig += 1
        else:
            pairs.append((d, orig))
        if i % 25 == 0:
            print(f"  originals resolved {len(pairs)}/{i}")
    report["pairs_with_original"] = len(pairs)
    report["no_original_found"] = no_orig
    print(f"pairs with original: {len(pairs)} (unresolved {no_orig})")

    issues = gh(f"repos/{args.repo}/issues", "state=all", f"labels={args.bug_label}", "per_page=100",
                "sort=created", "direction=desc", paginate=True)
    issues = [x for x in issues if "pull_request" not in x][: args.corpus]
    corpus = {}
    dropped = {"bot": 0, "template_only": 0, "is_query": 0}
    dup_numbers = {d["number"] for d, _ in pairs}
    for x in issues:
        if is_bot(x):
            dropped["bot"] += 1; continue
        if x["number"] in dup_numbers:
            dropped["is_query"] += 1; continue
        doc = to_doc(x)
        if len(doc["body"]) < 100:
            dropped["template_only"] += 1; continue
        corpus[doc["number"]] = doc
    report["corpus_from_bug_label"] = len(corpus)

    added = 0
    rows = []
    for d, orig in pairs:
        if orig not in corpus:
            o = gh(f"repos/{args.repo}/issues/{orig}")
            if "pull_request" in o or is_bot(o):
                continue
            doc = to_doc(o)
            if len(doc["body"]) < 100:
                continue
            corpus[orig] = doc
            added += 1
        q_body = clean(d.get("body"))
        if len(q_body) < 50:
            continue
        rows.append({"query": f"{d['title']}\n{q_body[:500]}", "relevant_ticket_ids": [f"GH-{orig}"],
                     "source_ticket_id": f"GH-{orig}", "difficulty": "duplicate", "source": "github",
                     "created_at": d["created_at"], "dup_number": d["number"]})
    report["originals_added_to_corpus"] = added
    report["corpus_total"] = len(corpus)
    report["eval_rows"] = len(rows)
    report["dropped"] = dropped

    (OUT / "data").mkdir(parents=True, exist_ok=True); (OUT / "eval").mkdir(parents=True, exist_ok=True)
    with open(OUT / "data" / "tickets.jsonl", "w", encoding="utf-8") as f:
        for doc in sorted(corpus.values(), key=lambda d: d["number"]):
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    with open(OUT / "eval" / "eval.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (OUT / "data" / "collect_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
