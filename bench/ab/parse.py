"""One run in, one JSON line out; or --report over a results file.

    printf '%s' "$claude_json" | python3 parse.py OUT ARM TASK REP EXPECT SECS
    python3 parse.py --report results.jsonl
"""
from __future__ import annotations

import json
import re
import statistics
import sys


def record(out: str, arm: str, task: str, rep: str, expect: str, secs: str) -> None:
    try:
        d = json.loads(sys.stdin.read())
    except Exception:
        d = {}
    u = d.get("usage") or {}
    answer = str(d.get("result") or "")
    tokens = sum(int(u.get(k) or 0) for k in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens"))
    row = {"arm": arm, "task": task, "rep": int(rep), "tokens": tokens,
           "turns": d.get("num_turns"), "cost_usd": d.get("total_cost_usd"),
           "secs": int(secs), "correct": bool(re.search(expect, answer, re.I)),
           "answer": answer[:300]}
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  {arm} {task} rep{rep}: {tokens:>8} tok  correct={row['correct']}  {secs}s",
          flush=True)


def report(path: str) -> None:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if "tokens" in r]
    if not rows:
        print("no runs")
        return
    print(f"\n{'task':<8}{'A tok':>10}{'B tok':>10}{'A ok':>7}{'B ok':>7}")
    for t in sorted({r["task"] for r in rows}):
        cell = {}
        for arm in "AB":
            rs = [r for r in rows if r["task"] == t and r["arm"] == arm]
            cell[arm] = (statistics.median(r["tokens"] for r in rs) if rs else 0,
                         sum(r["correct"] for r in rs), len(rs))
        a, b = cell["A"], cell["B"]
        print(f"{t:<8}{a[0]:>10,.0f}{b[0]:>10,.0f}{a[1]:>4}/{a[2]:<2}{b[1]:>4}/{b[2]:<2}")
    print()
    for arm, label in (("A", "recall on "), ("B", "recall off")):
        rs = [r for r in rows if r["arm"] == arm]
        toks = [r["tokens"] for r in rs]
        ok = sum(r["correct"] for r in rs)
        turns = statistics.median(r["turns"] or 0 for r in rs)
        print(f"{label}: correct {ok}/{len(rs)}   median {statistics.median(toks):>9,.0f} tok"
              f"   mean {statistics.mean(toks):>9,.0f}   median turns {turns:.0f}")
    # Noise: the widest spread inside one (task, arm) cell across repeats. If
    # this rivals the difference between arms, the difference is not a result.
    worst = 0.0
    for t in {r["task"] for r in rows}:
        for arm in "AB":
            v = [r["tokens"] for r in rows if r["task"] == t and r["arm"] == arm]
            if len(v) > 1 and statistics.median(v):
                worst = max(worst, (max(v) - min(v)) / statistics.median(v) * 100)
    print(f"\nworst within-cell token spread across repeats: {worst:.0f}%")
    wrong = [r for r in rows if not r["correct"]]
    if wrong:
        print(f"wrong answers ({len(wrong)}):")
        for r in wrong[:10]:
            print(f"  {r['arm']} {r['task']} rep{r['rep']}: {r['answer'][:90]}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--report"]:
        report(sys.argv[2])
    else:
        record(*sys.argv[1:7])
