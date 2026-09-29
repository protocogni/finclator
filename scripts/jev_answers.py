"""Run Jev (full question set: is_call + per-asset stance + horizon) on a pending JSONL → data/jev_raw_<tag>.jsonl.
Direct API, 32 workers, .env loaded.
Usage: PYTHONPATH=. .venv/bin/python scripts/jev_answers.py <pending.jsonl> <tag>"""
import json
import os
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
os.environ["JEV_WORKERS"] = "32"
src, tag = Path(sys.argv[1]), sys.argv[2]
sys.argv = [sys.argv[0]]  # score_jev reads argv only under __main__; we import its helpers
from scripts import score_jev as sj  # noqa: E402

rows = [json.loads(ln) for ln in open(src) if ln.strip()]
print(f"{len(rows)} tweets → Jev {sj.MODEL} via {sj.URL}", flush=True)
t0 = time.time()
raw, errors, lat, tokens = {}, 0, [], 0


def safe(t):
    try:
        return t["id"], sj.ask(t)
    except Exception as e:  # noqa: BLE001
        return t["id"], e


with ThreadPoolExecutor(max_workers=32) as pool:
    for tid, res in pool.map(safe, rows):
        if isinstance(res, Exception):
            errors += 1
            print("  error", tid, str(res)[:160], flush=True)
            continue
        ans, dt = res
        raw[tid] = ans
        lat.append(dt)
        tokens += ans.get("usage", {}).get("input_tokens", 0)
wall = time.time() - t0
out = ROOT / f"data/jev_raw_{tag}.jsonl"
with open(out, "w") as f:
    for t in rows:
        if t["id"] in raw:
            f.write(json.dumps({"id": t["id"], "text": t["text"], "assets_hint": t["assets_hint"], "raw": raw[t["id"]]},
                               ensure_ascii=False) + "\n")
print(f"{len(raw)} answered, {errors} errors, wall {wall:.1f}s, {60 * len(raw) / wall:.0f} tw/min, median latency "
      f"{sorted(lat)[len(lat) // 2]:.2f}s, input tokens {tokens:,} (≈ ${tokens / 1e6 * 0.042:.4f}) → {out}", flush=True)
