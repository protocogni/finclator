"""Label a pending JSONL with Claude Opus 5.5 through `hermes chat` (the interactive assistant path used for every
frontier gold set), carrying the verbatim classifier SYSTEM prompt from src/classify.py.

Usage: PYTHONPATH=. .venv/bin/python scripts/label_frontier.py <pending.jsonl> <out_labels.jsonl> [chunk=40] [workers=4]
Writes one label line per tweet: {"id", "is_call", "calls":[{asset, direction, horizon, confidence, price_target, quote}]}.
Chunks whose answer covers < 90 % of their ids are retried once. Progress → stdout; per-chunk raw answers kept in
$TMPDIR/opus_chunks/ for inspection.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.classify import SYSTEM

MODEL = os.environ.get("OPUS_MODEL", "claude-opus-5-5")
REASONING = os.environ.get("OPUS_REASONING", "high")
src, out = Path(sys.argv[1]), Path(sys.argv[2])
CHUNK = int(sys.argv[3]) if len(sys.argv) > 3 else 40
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 4
WORK = Path(os.environ.get("TMPDIR", "/tmp")) / "opus_chunks"
WORK.mkdir(parents=True, exist_ok=True)

rows = [json.loads(ln) for ln in open(src) if ln.strip()]
chunks = [rows[i:i + CHUNK] for i in range(0, len(rows), CHUNK)]
print(f"{len(rows)} tweets → {len(chunks)} chunks of ≤{CHUNK}, model={MODEL}, reasoning={REASONING}", flush=True)

TASK = """
You are labeling a batch of tweets for a research dataset. Apply the rules above EXACTLY as written to EVERY tweet
below, independently. Do not use any tools. Do not explain. Output ONLY JSON Lines: one line per input tweet, in the
same order, of the form
{"id": "<id>", "is_call": true|false, "calls": [{"asset": "BTC|GOLD|SPX", "direction": "BUY|SELL|NEUTRAL", "horizon": "SHORT|MEDIUM|LONG", "confidence": 0.0-1.0, "price_target": number|null, "quote": "<exact substring of the tweet text>"}]}
with "calls": [] when is_call is false. Every id below must appear exactly once. The quote must be copied verbatim
from the tweet text (same characters, no paraphrase). No markdown fences, no commentary before or after.

TWEETS (JSON, one per line):
"""


def ask(i: int, chunk: list[dict], attempt: int) -> list[dict]:
    q = WORK / f"chunk_{i:03d}_q.txt"
    q.write_text(SYSTEM + "\n" + TASK + "".join(
        json.dumps({"id": t["id"], "author": "@" + t["handle"], "date": t["created_at"][:10],
                    "assets_mentioned": t["assets_hint"], "text": html.unescape(t["text"])[:3000]},
                   ensure_ascii=False) + "\n" for t in chunk))
    t0 = time.time()
    r = subprocess.run(["hermes", "chat", "-Q", "--oneshot", "--ignore-rules", "--source", "tool", "-m", MODEL,
                        "--reasoning", REASONING, "--query-file", str(q)],
                       cwd=WORK, capture_output=True, text=True, timeout=1500)
    (WORK / f"chunk_{i:03d}_a{attempt}.txt").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr[-2000:])
    want = {t["id"] for t in chunk}
    got: dict[str, dict] = {}
    for ln in r.stdout.splitlines():
        ln = ln.strip().strip("`")
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("id") in want:
            got[d["id"]] = d
    print(f"  chunk {i:02d} attempt {attempt}: {len(got)}/{len(chunk)} ids in {time.time() - t0:.0f}s "
          f"(rc={r.returncode})", flush=True)
    return [got[t["id"]] for t in chunk if t["id"] in got]


def label(i_chunk):
    i, chunk = i_chunk
    res = ask(i, chunk, 1)
    if len(res) < 0.9 * len(chunk):
        res2 = ask(i, chunk, 2)
        if len(res2) > len(res):
            res = res2
    return res


t0 = time.time()
labels: list[dict] = []
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for res in pool.map(label, list(enumerate(chunks))):
        labels.extend(res)

# validate + normalise
text = {t["id"]: html.unescape(t["text"]) for t in rows}
bad_quote = dropped = 0
clean = []
for d in labels:
    calls = []
    for c in d.get("calls") or []:
        if c.get("asset") not in ("BTC", "GOLD", "SPX") or c.get("direction") not in ("BUY", "SELL", "NEUTRAL") \
                or c.get("horizon") not in ("SHORT", "MEDIUM", "LONG"):
            dropped += 1
            continue
        qte = c.get("quote") or ""
        if qte and re.sub(r"\s+", " ", qte) not in re.sub(r"\s+", " ", text[d["id"]]):
            bad_quote += 1
        pt = c.get("price_target")
        try:
            pt = float(pt) if pt not in (None, "", "null") else None
        except (TypeError, ValueError):
            pt = None
        calls.append({"asset": c["asset"], "direction": c["direction"], "horizon": c["horizon"],
                      "confidence": float(c.get("confidence", 0.5)), "price_target": pt, "quote": qte})
    clean.append({"id": d["id"], "is_call": bool(d.get("is_call")) and bool(calls), "calls": calls})
with open(out, "w") as f:
    for d in clean:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")
n_calls = sum(len(d["calls"]) for d in clean)
print(f"\nwrote {len(clean)}/{len(rows)} labels → {out}  calls={n_calls} is_call={sum(d['is_call'] for d in clean)} "
      f"dropped_malformed={dropped} quote_not_substring={bad_quote}  wall {time.time() - t0:.0f}s", flush=True)
