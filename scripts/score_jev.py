"""Score TypeSafe Jev (via Vercel AI Gateway) as a tweet→call classifier against a frontier-labeled gold set.

Jev is a System One model: it answers typed questions with probabilities, no text. So the classifier prompt is
decomposed into questions — is_call (noul) and per-asset stance / horizon (choice) — and it cannot produce
`quote` or `price_target`. Usage: PYTHONPATH=. .venv/bin/python scripts/score_jev.py [gold.jsonl] [N]
Auth: AI_GATEWAY_API_KEY, else VERCEL_OIDC_TOKEN from the file at FINCLATOR_GATEWAY_ENV (vercel env pull output).
Direct TypeSafe API instead of the gateway: JEV_ROUTE=direct (uses TYPESAFE_API_KEY, api.typesafe.ai, jev-latest)."""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from src.db import connect

ROUTE = os.environ.get("JEV_ROUTE", "gateway")
if ROUTE == "direct":
    URL = "https://api.typesafe.ai/v1/systemone"
    MODEL = "jev-latest"
else:
    URL = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
    MODEL = "typesafe-ai/jev"
ASSET_NAME = {"BTC": "Bitcoin (BTC)", "GOLD": "gold (altın, XAU)", "SPX": "the S&P 500 / US equities / Nasdaq (SPX)"}

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
    if ROUTE == "direct":
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise SystemExit("JEV_ROUTE=direct needs TYPESAFE_API_KEY")
        return os.environ["TYPESAFE_API_KEY"]
    if os.environ.get("AI_GATEWAY_API_KEY"):
        return os.environ["AI_GATEWAY_API_KEY"]
    p = os.environ.get("FINCLATOR_GATEWAY_ENV", "")
    for ln in open(p):
        if ln.startswith("VERCEL_OIDC_TOKEN="):
            return ln.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no AI_GATEWAY_API_KEY / VERCEL_OIDC_TOKEN")


TOKEN = _token()


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
        q[f"horizon_{a}"] = {
            "type": "choice",
            "instructions": {
                "question": f"If the author makes a call on {ASSET_NAME[a]}, what time horizon does the author's frame imply?",
                "rules": [
                    "SHORT (0-3 months): technical/level trading, daily closes, chart patterns, nearby levels, intraday, "
                    "this week, stops, hedges, a one-line reaction to the current move",
                    "MEDIUM (3-12 months): undated directional expectation with no level game and no structural argument, "
                    "a price target with no date, 'this year', 'coming months', a dated peak within the year",
                    "LONG (1-5 years): valuation / structural / cycle / generational theses, every bubble call, "
                    "'orta-uzun vade', debasement, decoupling, 'hold for years', 'bears will never win'",
                ],
            },
            "criteria": {"SHORT": "0-3 months", "MEDIUM": "3-12 months", "LONG": "1-5 years"},
        }
    return q


def ask(t) -> tuple[dict, float]:
    assets = [a for a in (t["assets_hint"] or "").split(",") if a in ASSET_NAME] or list(ASSET_NAME)
    body = {"model": MODEL, "state": {"author": "@" + t["handle"], "date": t["created_at"][:10],
                                      "assets_mentioned": assets, "tweet": t["text"][:3000]},
            "questions": questions(assets)}
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    for attempt in range(8):
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r), time.time() - t0
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529):
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{e.code} {e.read()[:300]}") from e
    raise RuntimeError("rate limited")


def to_pred(ans: dict, assets: list[str], thr: float) -> dict:
    a = ans["answers"]
    p_call = a["is_call"]["noul"]
    calls = []
    for x in assets:
        st = a.get(f"stance_{x}")
        if not st or st["choice"] == "none":
            continue
        calls.append({"asset": x, "direction": {"up": "BUY", "down": "SELL", "neutral": "NEUTRAL"}[st["choice"]],
                      "horizon": a[f"horizon_{x}"]["choice"], "confidence": round(st["confidence"], 3),
                      "p_stance": st["probabilities"], "p_horizon": a[f"horizon_{x}"]["probabilities"]})
    is_call = p_call >= thr and bool(calls)
    return {"is_call": is_call, "p_call": round(p_call, 3), "calls": calls if is_call else [], "stances": calls}


if __name__ == "__main__":
    gold_path = sys.argv[1] if len(sys.argv) > 1 else "data/labels_holdout.jsonl"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9
    gold = {}
    for line in open(gold_path):
        r = json.loads(line)
        gold[r["id"]] = r
    conn = connect()
    rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE tweet_id IN (%s) ORDER BY tweet_id" %
                        ",".join("?" * len(gold)), list(gold)).fetchall()[:limit]
    print(f"route={ROUTE} model={MODEL} gold={gold_path} n={len(rows)} "
          f"workers={os.environ.get('JEV_WORKERS', '2')}", flush=True)

    t0 = time.time()
    raw: dict = {}
    lat: list[float] = []
    tokens = Counter()
    errors = 0

    def safe(t):
        try:
            return t["id"], ask(t)
        except Exception as e:  # noqa: BLE001
            return t["id"], e

    with ThreadPoolExecutor(max_workers=int(os.environ.get("JEV_WORKERS", "2"))) as pool:
        for tid, res in pool.map(safe, rows):
            if isinstance(res, Exception):
                errors += 1
                print("  error", tid, str(res)[:160], flush=True)
                continue
            ans, dt = res
            raw[tid] = ans
            lat.append(dt)
            tokens["in"] += ans.get("usage", {}).get("input_tokens", 0)
    wall = time.time() - t0
    tag = os.path.basename(gold_path).replace("labels_", "").replace(".jsonl", "")
    with open(f"data/tune_jev_{tag}.jsonl", "w") as out:
        for t in rows:
            if t["id"] in raw:
                out.write(json.dumps({"id": t["id"], "text": t["text"], "assets_hint": t["assets_hint"],
                                      "gold": gold[t["id"]], "raw": raw[t["id"]]}, ensure_ascii=False) + "\n")

    print(f"\n{len(raw)} answered, {errors} errors, wall {wall:.1f}s, {60 * len(raw) / wall:.0f} tw/min, "
          f"median latency {sorted(lat)[len(lat) // 2]:.2f}s, input tokens {tokens['in']:,} "
          f"(≈ ${tokens['in'] / 1e6 * 0.042:.4f})")
    print(f"\n{'thr':>5}{'is_call':>9}{'prec':>7}{'rec':>7}{'F1':>7}{'dir':>7}{'hor':>7}{'both':>6}")
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        c = Counter()
        for t in rows:
            if t["id"] not in raw:
                continue
            assets = [a for a in (t["assets_hint"] or "").split(",") if a in ASSET_NAME] or list(ASSET_NAME)
            g, p = gold[t["id"]], to_pred(raw[t["id"]], assets, thr)
            gi, pi = bool(g["is_call"]), p["is_call"]
            c["n"] += 1
            c["acc"] += gi == pi
            c["tp"] += gi and pi
            c["fp"] += pi and not gi
            c["fn"] += gi and not pi
            gc = {x["asset"]: x for x in g.get("calls", [])}
            pc = {x["asset"]: x for x in p["calls"]}
            for a in set(gc) & set(pc):
                c["both"] += 1
                c["dir"] += gc[a]["direction"] == pc[a]["direction"]
                c["hor"] += gc[a]["horizon"] == pc[a]["horizon"]
        pr = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0
        rc = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0
        f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0
        d = c["dir"] / c["both"] if c["both"] else float("nan")
        h = c["hor"] / c["both"] if c["both"] else float("nan")
        print(f"{thr:>5.1f}{c['acc'] / c['n']:>9.3f}{pr:>7.2f}{rc:>7.2f}{f1:>7.2f}{d:>7.2f}{h:>7.2f}{c['both']:>6}")
