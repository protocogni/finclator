"""Measure regex prefilter recall: sample English tweets the prefilter REJECTED, run the Jev gate on them with all
three assets, and report how many come back as calls (p_call >= threshold). Writes data/prefilter_rejects_jev.jsonl.
Usage: PYTHONPATH=. .venv/bin/python scripts/prefilter_recall.py [N] [--seed S]"""
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for ln in (ROOT / ".env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))
os.environ["JEV_ROUTE"] = "direct"

args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(args[0]) if args else 1000
SEED = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 20261001
sys.argv = [sys.argv[0]]
from scripts import score_jev as sj  # noqa: E402
from src.db import connect  # noqa: E402
from src.gate import THRESHOLD  # noqa: E402
from src.prefilter import is_relevant  # noqa: E402

TR_CHARS = re.compile(r"[ğışçöüİĞŞÇÖÜ]")
conn = connect()
rows = conn.execute("SELECT t.tweet_id AS id, t.handle, t.created_at, t.text, a.language FROM tweets t "
                    "JOIN accounts a ON a.handle=t.handle WHERE t.relevant=0 AND t.is_reply=0").fetchall()
total_rej = len(rows)
en = [r for r in rows if not TR_CHARS.search(r["text"]) and (r["language"] or "en") != "tr" and 30 <= len(r["text"]) <= 1200]
# re-check against the CURRENT regex so a stale flag can't leak
en = [r for r in en if not is_relevant(r["text"])[0]]
print(f"rejected originals: {total_rej:,} | EN candidates after re-check: {len(en):,}")
random.seed(SEED)
random.shuffle(en)
picked = en[:N]
print(f"sampling {len(picked)} → Jev, all three assets, threshold {THRESHOLD}")


def safe(r):
    t = {"id": r["id"], "handle": r["handle"], "created_at": r["created_at"], "assets_hint": "", "text": r["text"]}
    try:
        ans, _ = sj.ask(t)
        return r, ans, None
    except Exception as e:  # noqa: BLE001
        return r, None, str(e)[:160]


t0 = time.time()
out = ROOT / "data/prefilter_rejects_jev.jsonl"
hits, errors = [], 0
with open(out, "w") as f, ThreadPoolExecutor(max_workers=32) as pool:
    for r, ans, err in pool.map(safe, picked):
        if err:
            errors += 1
            continue
        p = float(ans["answers"]["is_call"]["noul"])
        pred = sj.to_pred(ans, list(sj.ASSET_NAME), THRESHOLD)
        rec = {"id": r["id"], "handle": r["handle"], "created_at": r["created_at"], "text": r["text"], "p_call": p,
               "pred": pred, "raw": ans}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if pred["is_call"]:
            hits.append(rec)
print(f"done in {time.time()-t0:.0f}s, {errors} errors → {out}")
print(f"gate passes (p_call >= {THRESHOLD} and a stance != none): {len(hits)} / {len(picked)-errors} = "
      f"{100*len(hits)/max(1,len(picked)-errors):.1f}%")
for b in (0.5, 0.7, 0.9):
    print(f"  of which p_call >= {b}: {sum(1 for h in hits if h['p_call'] >= b)}")
print("\nTop 40 by p_call:")
for h in sorted(hits, key=lambda h: -h["p_call"])[:40]:
    calls = ",".join(f"{c['asset']}:{c['direction']}" for c in h["pred"].get("calls", []))
    print(f"  {h['p_call']:.2f} {calls} @{h['handle']} {h['created_at'][:10]} https://x.com/{h['handle']}/status/{h['id']}")
    print("       " + h["text"].replace("\n", " ")[:220])
