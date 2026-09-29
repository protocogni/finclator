"""Gate EVERY regex-rejected original through Jev (all three assets), storing rows in `gate` like production.
Resumable: `gate.ensure` skips tweets that already have a row. Then print the comparison vs the regex-accepted set.
Usage: PYTHONPATH=. .venv/bin/python scripts/gate_all.py [--stats-only]"""
import json
import re
import sys
import time
from collections import Counter

from src import gate
from src.db import connect, log

TR = re.compile(r"[ğışçöüİĞŞÇÖÜ]")
conn = connect()


def lang(text, acct_lang):
    return "tr" if (acct_lang == "tr" or TR.search(text)) else "en"


if "--stats-only" not in sys.argv:
    rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE relevant=0 AND is_reply=0 "
                        "AND tweet_id NOT IN (SELECT tweet_id FROM gate) ORDER BY created_at DESC").fetchall()
    log(f"gate_all: {len(rows):,} regex-rejected originals without a gate row")
    t0 = time.time()
    gate.ensure(conn, rows)
    log(f"gate_all: finished in {time.time() - t0:.0f}s")

# --- comparison ---
q = ("SELECT t.tweet_id AS id, t.relevant, t.text, a.language, g.p_call, g.stances FROM tweets t "
     "JOIN accounts a ON a.handle=t.handle JOIN gate g ON g.tweet_id=t.tweet_id WHERE t.is_reply=0")
stats: dict = {}
for r in conn.execute(q):
    key = ("regex-accepted" if r["relevant"] == 1 else "regex-rejected", lang(r["text"], r["language"]))
    s = stats.setdefault(key, {"n": 0, "pass": 0, "p50": 0, "p70": 0, "p90": 0, "assets": Counter()})
    st = json.loads(r["stances"] or "{}")
    ok = gate.passes(r["p_call"], st)
    s["n"] += 1
    s["pass"] += ok
    for b in (50, 70, 90):
        s[f"p{b}"] += ok and r["p_call"] >= b / 100
    if ok:
        for a, c in st.items():
            if c != "none":
                s["assets"][a] += 1
print(f"\n{'set':16} {'lang':4} {'n':>8} {'pass':>7} {'rate':>6} {'p≥.5':>6} {'p≥.7':>6} {'p≥.9':>6}  stances of passes")
for (k, lg), s in sorted(stats.items()):
    print(f"{k:16} {lg:4} {s['n']:>8,} {s['pass']:>7,} {100*s['pass']/s['n']:>5.1f}% {s['p50']:>6,} {s['p70']:>6,} "
          f"{s['p90']:>6,}  {dict(s['assets'].most_common())}")
acc = sum(s["pass"] for (k, _), s in stats.items() if k == "regex-accepted")
rej = sum(s["pass"] for (k, _), s in stats.items() if k == "regex-rejected")
print(f"\ngate passes: regex-accepted {acc:,} · regex-rejected {rej:,} → regex recall on gate passes ≈ {100*acc/(acc+rej):.1f}%")
print("(gate passes are candidates, not calls; Jev precision on accepted EN was 0.63 vs Fable)")
