"""Agreement test: re-classify frontier-labeled tweets with the configured model and compare.
Usage: PYTHONPATH=. .venv/bin/python scripts/agreement.py [N] [gold.jsonl]   (FINCLATOR_MODEL=<tag> to test another)
Default gold = data/labels_backfill.jsonl (the prompt-tuning set); pass data/labels_holdout.jsonl for the out-of-sample check.
"""
import json
import sys
import time
from collections import Counter

from src.classify import make_classifier
from src.db import connect

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
gold_path = sys.argv[2] if len(sys.argv) > 2 else "data/labels_backfill.jsonl"
gold = {}
for line in open(gold_path):
    r = json.loads(line)
    gold[r["id"]] = r
conn = connect()
rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE tweet_id IN (%s)" %
                    ",".join("?" * len(gold)), list(gold)).fetchall()[:limit]
run, model = make_classifier()
print(f"model={model}  n={len(rows)}", flush=True)

t0 = time.time()
c = Counter()
per_asset_dir = Counter()
horizon = Counter()
disagreements = []
out = open("data/agreement_preds.jsonl", "w")  # every prediction next to gold, for offline prompt-tuning
for i, t in enumerate(rows, 1):
    g = gold[t["id"]]
    p = run(t)
    out.write(json.dumps({"id": t["id"], "text": t["text"], "assets_hint": t["assets_hint"], "gold": g, "pred": p},
                         ensure_ascii=False) + "\n")
    c["n"] += 1
    c["is_call_agree"] += int(bool(p.get("is_call")) == bool(g["is_call"]))
    gcalls = {x["asset"]: x for x in g.get("calls", [])}
    pcalls = {x.get("asset"): x for x in p.get("calls", []) if x.get("asset") in ("BTC", "GOLD", "SPX")}
    for a in set(gcalls) | set(pcalls):
        per_asset_dir["n"] += 1
        if a in gcalls and a in pcalls:
            per_asset_dir["both"] += 1
            per_asset_dir["dir_agree"] += int(gcalls[a]["direction"] == pcalls[a].get("direction"))
            horizon["n"] += 1
            horizon["agree"] += int(gcalls[a]["horizon"] == pcalls[a].get("horizon"))
        elif a in gcalls:
            per_asset_dir["missed"] += 1
        else:
            per_asset_dir["extra"] += 1
    if bool(p.get("is_call")) != bool(g["is_call"]) and len(disagreements) < 12:
        disagreements.append((g["is_call"], p.get("is_call"), t["text"][:110].replace("\n", " ")))
    if i % 25 == 0:
        print(f"  {i}/{len(rows)}  {(time.time()-t0)/i:.1f}s/tweet", flush=True)

dt = time.time() - t0
print(f"\n{model}: {c['n']} tweets in {dt:.0f}s ({dt/c['n']:.1f}s each)")
print(f"is_call agreement:   {c['is_call_agree']}/{c['n']} = {c['is_call_agree']/c['n']:.1%}")
b = per_asset_dir
print(f"asset-calls: gold∪pred={b['n']}  both={b['both']}  missed={b['missed']}  extra={b['extra']}")
if b["both"]:
    print(f"direction agreement (when both called): {b['dir_agree']}/{b['both']} = {b['dir_agree']/b['both']:.1%}")
    print(f"horizon agreement   (when both called): {horizon['agree']}/{horizon['n']} = {horizon['agree']/horizon['n']:.1%}")
print("\nsample is_call disagreements (gold, pred, text):")
for d in disagreements:
    print("  ", d)
