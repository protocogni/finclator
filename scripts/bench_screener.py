"""Stage-1 screener test: can a small model reject non-calls with ~zero missed calls?
Reports recall (of gold calls), the pass-through rate (share of tweets sent to stage 2), and speed.
Usage: PYTHONPATH=. .venv/bin/python scripts/bench_screener.py <ollama-tag> [--gold path] [--n N]
"""
import json
import sys
import time
import urllib.request

from src.classify import SYSTEM, _user_msg
from src.db import connect

tag = sys.argv[1]
gold_path = sys.argv[sys.argv.index("--gold") + 1] if "--gold" in sys.argv else "data/labels_backfill.jsonl"
limit = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 10**9
gold = {json.loads(l)["id"]: json.loads(l) for l in open(gold_path)}
rows = connect().execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE tweet_id IN (%s)" %
                         ",".join("?" * len(gold)), list(gold)).fetchall()[:limit]

SCREEN = SYSTEM + """

SCREENING MODE. You are a first-pass filter; a stronger model re-checks everything you keep. Answer ONE character.
Reply 0 when the tweet is one of these and nothing more:
  - a news item, data release, statistic, ETF flow, holdings/purchase report ("X bought N bitcoins")
  - a description of a past or current price move ("hit a new high", "yeni zirve", "rallied", "%2 negatif")
  - a question or poll, a video/podcast/segment title, a bare chart caption or link
  - a joke, a sales pitch with no direction, or a stance only about some other stock/coin
  - macro/politics/Fed commentary that never says where BTC, gold or US stocks go next
Reply 1 when the author gives ANY forward view or advice on BTC, gold or US stocks — including implied ones:
  hold / buy / sell / take profit / don't worry ("sahip çıkın", "kaygılanmayın", "kar al"), an expectation
  ("bekliyorum", "görülecek", "gider", "will", "should", "bullish", "bearish"), a level or target, a held
  position ("we remain short"), bubble/valuation talk ("balon", "bubble", "aşırı değerli"), or "still in the
  bull cycle". Keep it even when the view sits inside a long macro recap or is sarcastic/idiomatic."""


def screen(t) -> tuple[bool, int]:
    body = json.dumps({"model": tag, "stream": False, "keep_alive": "1h", "think": False,
                       "options": {"temperature": 0, "num_ctx": 6144, "num_predict": 3},
                       "messages": [{"role": "system", "content": SCREEN}, {"role": "user", "content": _user_msg(t)}]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:11434/api/chat", body,
                                                                {"Content-Type": "application/json"}), timeout=120))
    c = (r["message"]["content"] or "").strip()
    return ("1" in c[:3]) or (c[:1] not in ("0", "")), r["eval_count"]


screen(rows[0])
t0 = time.time()
tp = fn = kept = 0
missed = []
for t in rows:
    keep, _ = screen(t)
    g = gold[t["id"]]["is_call"]
    kept += keep
    if g and keep:
        tp += 1
    elif g and not keep:
        fn += 1
        missed.append(t["text"][:100].replace("\n", " "))
dt = time.time() - t0
print(f"{tag}: n={len(rows)} {len(rows)/dt*60:.0f}/min ({dt/len(rows):.2f}s)  recall={tp}/{tp+fn}={tp/max(1,tp+fn):.1%}  "
      f"pass-through={kept}/{len(rows)}={kept/len(rows):.0%}")
for m in missed[:8]:
    print("  MISSED:", m)
