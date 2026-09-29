"""Batched classification: agreement vs frontier labels and throughput, for several batch sizes.
Usage: FINCLATOR_MODEL_BASE_URL=... FINCLATOR_MODEL=... PYTHONPATH=. .venv/bin/python scripts/bench_batch.py [sizes...] [--gold path]
"""
import json
import sys
import time
from collections import Counter

from src import classify
from src.db import connect

args = [a for a in sys.argv[1:] if not a.startswith("--") and not (sys.argv[sys.argv.index(a) - 1].startswith("--"))]
gold_path = sys.argv[sys.argv.index("--gold") + 1] if "--gold" in sys.argv else "data/labels_backfill.jsonl"
limit = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 10**9
sizes = [int(a) for a in args] or [1, 4, 8, 12]
gold = {json.loads(l)["id"]: json.loads(l) for l in open(gold_path)}
conn = connect()
rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE tweet_id IN (%s)" %
                    ",".join("?" * len(gold)), list(gold)).fetchall()[:limit]
single, model = classify.make_classifier()
batch, _ = classify.make_batch_classifier()
single(rows[0])  # warm
print(f"model={model} gold={gold_path} n={len(rows)}")


def score(preds):
    c = Counter()
    for t, p in zip(rows, preds):
        g = gold[t["id"]]
        c["n"] += 1
        c["is_call"] += int(bool(p.get("is_call")) == bool(g["is_call"]))
        gc = {x["asset"]: x for x in g.get("calls", [])}
        pc = {x.get("asset"): x for x in p.get("calls", []) if x.get("asset") in ("BTC", "GOLD", "SPX")}
        for a in set(gc) | set(pc):
            if a in gc and a in pc:
                c["both"] += 1
                c["dir"] += int(gc[a]["direction"] == pc[a].get("direction"))
                c["hor"] += int(gc[a]["horizon"] == pc[a].get("horizon"))
            elif a in gc:
                c["missed"] += 1
            else:
                c["extra"] += 1
    return c


for bs in sizes:
    t0 = time.time()
    if bs == 1:
        preds = [single(t) for t in rows]
    else:
        preds = []
        for i in range(0, len(rows), bs):
            preds += batch(rows[i:i + bs])
    dt = time.time() - t0
    c = score(preds)
    print(f"batch={bs:2d}  {len(rows)/dt*60:5.0f}/min ({dt/len(rows):.2f}s/tweet)  is_call={c['is_call']/c['n']:.1%}  "
          f"dir={c['dir']}/{c['both']}  hor={c['hor']}/{c['both']}  missed={c['missed']} extra={c['extra']}", flush=True)
