"""Stage 2a — the Jev is_call gate (TypeSafe System One decision model, direct API).

Jev answers typed questions with calibrated probabilities and no text, so it cannot produce `quote` or
`price_target`; it sits in FRONT of the text classifier and decides which relevant tweets are worth a GPU call.
Measured on 1,000 English tweets vs Fable 5.1 labels (docs/JEV_EXPERIMENTS.md, scripts/compare_four.py): at
p_call ≥ 0.2 the gate keeps 20 % of tweets and the hybrid loses 2 pts of call recall vs the ungated text model
(0.77 vs 0.79) while precision rises 0.66 → 0.79; at 0.3 it lost 7 pts. 0.24 s latency, no rate-limit errors up to
32 workers on api.typesafe.ai (the Vercel gateway throttles at ~35 tw/min — do not route bulk through it).

Results live in `gate(tweet_id, model, p_call, stances, at)` keyed by the versioned Jev id, so a tweet is gated
once per Jev version; `passes()` is the single rule the hybrid classifier applies.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .db import log

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")           # request alias; rows are stored under response.model
THRESHOLD = float(os.environ.get("JEV_THRESHOLD", "0.2"))     # 1,000-tweet sweep: 0.2 costs 2 pts recall, 0.3 cost 7, 0.5 halves it
WORKERS = int(os.environ.get("JEV_WORKERS", "32"))
MAX_TEXT = 3000
ASSET_NAME = {"BTC": "Bitcoin (BTC)", "GOLD": "gold (altın, XAU)", "SPX": "the S&P 500 / US equities / Nasdaq (SPX)"}

# The classifier SYSTEM prompt (src/classify.py) decomposed into typed questions. Keep the two in step.
NOT_CALL = [
    "reporting a move that already happened or is happening now (new high, rally this morning, yeni zirve, tarihi rekor)",
    "a question, poll, podcast/video segment title or headline with no answer",
    "quoting or forwarding someone else's view (analyst, firm, politician) without the author adopting it",
    "sentiment / positioning / flow / indicator observations with no explicit price prediction attached",
    "macro or news commentary, explanations of why a move happened, commentary on other people's mood or forecasts",
    "a bare caption for an image or chart with no words of opinion",
    "a stance on a different asset (altcoin, miner, MSTR, a stock) that merely carries a #BTC/#gold/#nasdaq tag",
    "pure chart narration with only a conditional outcome and no base case",
    "a mood-only video title with an asset hashtag list (which asset, which way is unknowable)",
]
IS_CALL = [
    "bubble talk (balon, bubble, aşırı değerli) is a SELL call",
    "exclamatory conviction is a call: 'Pat-la-ya-cak!' → up; 'bir dönem kapandı' → down; 'yol uzun' → up",
    "'bears will never win', 'daha yüksek seviyeler görülecek' → up",
    "sales pitches with an explicit stance count ('switch from fool's gold into the real thing' → BTC down, gold up)",
    "a stated base case ('getting closer to $93000', 'could continue as long as $73k holds') is a call",
]


def _token() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and (ROOT / ".env").exists():
        for ln in (ROOT / ".env").read_text().splitlines():
            if ln.startswith("TYPESAFE_API_KEY="):
                key = ln.split("=", 1)[1].strip().strip('"')
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY not set (env or .env)")
    return key


def questions(assets: list[str]) -> dict:
    q = {
        "is_call": {
            "type": "noul",
            "instructions": {
                "question": "Is this tweet the AUTHOR'S OWN explicit, forward-looking, falsifiable claim that the price of "
                            "Bitcoin, gold or the S&P 500 / US equities will go up, go down, or that the author is explicitly "
                            "neutral / two-sided / waiting? Tweets may be Turkish or English.",
                "not_a_call": NOT_CALL,
                "is_a_call_even_when_idiomatic": IS_CALL,
                "be_strict": "when in doubt, answer no",
            },
            "criteria": {"true": "the author states their own directional (or explicitly neutral) view on the price",
                         "false": "no explicit forward-looking stance of the author on the price"},
        }
    }
    for a in assets:
        q[f"stance_{a}"] = {
            "type": "choice",
            "instructions": {
                "question": f"What is the author's OWN forward-looking stance on the price of {ASSET_NAME[a]} in this tweet?",
                "rules": [
                    "only the author's own view counts; forwarded views, past moves, questions, chart captions → none",
                    "two-sided level conditionals ('above X positive, below X negative') → neutral",
                    "waiting to buy only after an expected crash → neutral",
                    "a pullback the author calls a correction inside an intact trend → the trend direction",
                    "a short-term bounce inside a larger opposite thesis → the dominant thesis",
                    "a price target above the current price → up, below → down",
                    "a hashtag or mention alone is not a stance",
                ],
            },
            "criteria": {
                "none": f"the author takes no forward-looking stance on {ASSET_NAME[a]} (not a call for this asset)",
                "up": "the author expects the price to rise (BUY)",
                "down": "the author expects the price to fall (SELL)",
                "neutral": "the author is explicitly neutral, two-sided or waiting",
            },
        }
    return q


def _assets(t) -> list[str]:
    return [a for a in (t["assets_hint"] or "").split(",") if a in ASSET_NAME] or list(ASSET_NAME)


def ask(t, token: str) -> dict:
    """One Jev request for one tweet → {"model", "p_call", "stances": {asset: choice}}. Retries 429/503/529."""
    assets = _assets(t)
    text = t["text"]
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + " […truncated]"
    body = {"model": MODEL, "state": {"author": "@" + t["handle"], "date": t["created_at"][:10],
                                      "assets_mentioned": assets, "tweet": text},
            "questions": questions(assets)}
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                ans = json.load(r)
            a = ans["answers"]
            return {"model": ans.get("model") or MODEL, "p_call": float(a["is_call"]["noul"]),
                    "stances": {x: a[f"stance_{x}"]["choice"] for x in assets if f"stance_{x}" in a}}
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503, 529):
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"jev {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"jev gave up: {last}")


def passes(p_call: float, stances: dict, threshold: float = THRESHOLD) -> bool:
    """The gate rule: probability over threshold AND at least one asset with a stance."""
    return p_call >= threshold and any(s != "none" for s in stances.values())


def lookup(conn, tweet_ids: list[str]) -> dict[str, dict]:
    """Stored gate rows for these tweets (any Jev version; newest wins)."""
    out: dict[str, dict] = {}
    for i in range(0, len(tweet_ids), 500):
        chunk = tweet_ids[i:i + 500]
        for r in conn.execute(f"SELECT tweet_id, model, p_call, stances FROM gate WHERE tweet_id IN ({','.join('?' * len(chunk))}) "
                              "ORDER BY at", chunk):
            out[r["tweet_id"]] = {"model": r["model"], "p_call": r["p_call"], "stances": json.loads(r["stances"] or "{}")}
    return out


def ensure(conn, rows) -> dict[str, dict]:
    """Gate every tweet in `rows` that has no gate row yet; returns {tweet_id: gate dict} for all of them.
    Writes happen on the calling thread; a failed request is logged and the tweet is left ungated (caller skips it)."""
    rows = list(rows)
    have = lookup(conn, [t["id"] for t in rows])
    todo = [t for t in rows if t["id"] not in have]
    if not todo:
        return have
    token = _token()
    log(f"gate: {len(todo):,} tweets → Jev ({WORKERS} workers, threshold {THRESHOLD})")
    t0 = time.time()
    done = failed = 0

    def safe(t):
        try:
            return t, ask(t, token), None
        except Exception as e:  # noqa: BLE001
            return t, None, e

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for t, res, err in pool.map(safe, todo):
            if err is not None:
                failed += 1
                if failed <= 5:
                    log(f"gate: {t['id']} failed: {type(err).__name__}: {str(err)[:120]}")
                continue
            conn.execute("INSERT OR REPLACE INTO gate(tweet_id, model, p_call, stances, at) VALUES(?,?,?,?,datetime('now'))",
                         (t["id"], res["model"], res["p_call"], json.dumps(res["stances"])))
            have[t["id"]] = res
            done += 1
            if done % 500 == 0:
                conn.commit()
                log(f"gate: {done:,}/{len(todo):,} ({60 * done / (time.time() - t0):.0f} tw/min, {failed} failed)")
    conn.commit()
    passed = sum(passes(g["p_call"], g["stances"]) for tid, g in have.items() if tid in {t["id"] for t in todo})
    log(f"gate: done {done:,} in {time.time() - t0:.0f}s, {failed} failed, {passed:,} passed")
    return have


if __name__ == "__main__":
    import sys

    from .db import connect

    conn = connect()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    q = "SELECT id, handle, created_at, text, assets_hint FROM tweets WHERE relevant=1 AND id NOT IN (SELECT tweet_id FROM gate) ORDER BY created_at DESC"
    rows = conn.execute(q + (f" LIMIT {limit}" if limit else "")).fetchall()
    print(f"{len(rows)} relevant tweets without a gate row")
    ensure(conn, rows)
