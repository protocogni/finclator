"""Score the production Qwen classifier (batch-4, terse, think off, 8 workers) on a pending JSONL — same code paths as
production (classify.make_batch_classifier) — and write preds to data/qwen_preds_<tag>.jsonl.
Usage: PYTHONPATH=. .venv/bin/python scripts/qwen_answers.py <pending.jsonl> <tag>"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("FINCLATOR_MODEL_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("FINCLATOR_BATCH_SIZE", "4")
os.environ.setdefault("FINCLATOR_WORKERS", "8")
os.environ.setdefault("FINCLATOR_GATE", "0")
from src import classify  # noqa: E402

src, tag = Path(sys.argv[1]), sys.argv[2]
rows = [json.loads(ln) for ln in open(src) if ln.strip()]
print(f"{len(rows)} tweets → {classify.TEXT_MODEL} batch={classify.BATCH_SIZE} workers={classify.WORKERS} "
      f"terse={classify.TERSE} think={classify.THINK}", flush=True)
single, model = classify.make_classifier()
t0 = time.time()
single(rows[0])  # warm-up (model load)
print(f"warm-up {time.time() - t0:.0f}s", flush=True)
run_batch, _ = classify.make_batch_classifier()
chunks = [rows[i:i + classify.BATCH_SIZE] for i in range(0, len(rows), classify.BATCH_SIZE)]
t0 = time.time()
preds = {}
done = 0
with ThreadPoolExecutor(max_workers=classify.WORKERS) as pool:
    for chunk, res in zip(chunks, pool.map(run_batch, chunks), strict=True):
        for t, p in zip(chunk, res, strict=True):
            preds[t["id"]] = p
        done += len(chunk)
        if done % 80 == 0:
            print(f"  {done}/{len(rows)}  {60 * done / (time.time() - t0):.0f} tw/min", flush=True)
dt = time.time() - t0
out = Path(f"data/qwen_preds_{tag}.jsonl")
with open(out, "w") as f:
    for t in rows:
        f.write(json.dumps({"id": t["id"], "text": t["text"], "assets_hint": t["assets_hint"], "pred": preds[t["id"]]},
                           ensure_ascii=False) + "\n")
n_calls = sum(len(p.get("calls", [])) for p in preds.values() if p.get("is_call"))
print(f"{len(preds)} tweets in {dt:.0f}s ({60 * len(preds) / dt:.0f} tw/min), is_call={sum(bool(p.get('is_call')) for p in preds.values())} "
      f"calls={n_calls} errors={sum('error' in p for p in preds.values())} → {out}", flush=True)
