"""Score one classifier configuration against a frontier-labeled gold set, with WORKERS-wide parallelism and
optional batching — the same code paths production uses. Prints one JSON summary line (prefixed `RESULT `).
Usage (env selects the variant): PYTHONPATH=. .venv/bin/python scripts/score_variant.py <name> [gold.jsonl] [N]
"""
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from src import classify
from src.db import connect

name = sys.argv[1]
gold_path = sys.argv[2] if len(sys.argv) > 2 else "data/labels_holdout.jsonl"
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
gold = {}
for line in open(gold_path):
    r = json.loads(line)
    gold[r["id"]] = r
conn = connect()
rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE tweet_id IN (%s) ORDER BY tweet_id" %
                    ",".join("?" * len(gold)), list(gold)).fetchall()[:limit]

# warm-up: one call so model load / prefix cache is not in the timing
single, model = classify.make_classifier()
single(rows[0])

t0 = time.time()
preds: dict = {}
if classify.BATCH_SIZE > 1:
    run_batch, _ = classify.make_batch_classifier()
    chunks = [rows[i:i + classify.BATCH_SIZE] for i in range(0, len(rows), classify.BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=classify.WORKERS) as pool:
        for chunk, res in zip(chunks, pool.map(run_batch, chunks), strict=True):
            for t, p in zip(chunk, res, strict=True):
                preds[t["id"]] = p
else:
    def safe(t):
        try:
            return t["id"], single(t)
        except Exception as e:  # noqa: BLE001
            return t["id"], {"is_call": False, "calls": [], "error": str(e)[:80]}
    with ThreadPoolExecutor(max_workers=classify.WORKERS) as pool:
        for tid, p in pool.map(safe, rows):
            preds[tid] = p
dt = time.time() - t0

c = Counter()
out = open(f"data/tune_{name}.jsonl", "w")
for t in rows:
    g, p = gold[t["id"]], preds[t["id"]]
    out.write(json.dumps({"id": t["id"], "text": t["text"], "gold": g, "pred": p}, ensure_ascii=False) + "\n")
    c["n"] += 1
    c["errors"] += int("error" in p)
    gi, pi = bool(g["is_call"]), bool(p.get("is_call"))
    c["is_call_agree"] += int(gi == pi)
    c["tp"] += int(gi and pi)
    c["fn"] += int(gi and not pi)
    c["fp"] += int(pi and not gi)
    gc = {x["asset"]: x for x in g.get("calls", [])}
    pc = {x.get("asset"): x for x in p.get("calls", []) if x.get("asset") in ("BTC", "GOLD", "SPX")}
    for a in set(gc) & set(pc):
        c["both"] += 1
        c["dir"] += int(gc[a]["direction"] == pc[a].get("direction"))
        c["hor"] += int(gc[a]["horizon"] == pc[a].get("horizon"))
prec = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0
rec = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0
res = {
    "variant": name, "model": model, "n": c["n"], "seconds": round(dt, 1), "s_per_tweet": round(dt / c["n"], 3),
    "tweets_per_min": round(60 * c["n"] / dt, 1), "errors": c["errors"],
    "is_call_acc": round(c["is_call_agree"] / c["n"], 3), "call_precision": round(prec, 3), "call_recall": round(rec, 3),
    "call_f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0,
    "both": c["both"], "dir_acc": round(c["dir"] / c["both"], 3) if c["both"] else None,
    "hor_acc": round(c["hor"] / c["both"], 3) if c["both"] else None,
    "env": {k: os.environ.get(k) for k in ("FINCLATOR_WORKERS", "FINCLATOR_BATCH_SIZE", "FINCLATOR_TERSE",
                                          "FINCLATOR_THINK", "FINCLATOR_NUM_CTX")},
}
print("RESULT " + json.dumps(res))
