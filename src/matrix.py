"""3x3 matrix: for each (asset, horizon) aggregate recent calls, weighted by trust × √confidence × recency.

Recency: exponential decay with half-life = 1/3 of the horizon's maturity window, and a hard cutoff at the
full window (a SHORT call older than 3 months is stale by definition).
One vote per account per cell: only its latest call inside the window counts (`n_calls` = calls in window,
`n_accounts` = votes). A cell (or school sub-label) with fewer than MIN_ACCOUNTS voters is N/A.
School aggregation: sum of member weights (a one-person school cannot dominate). `top_handle`/`top_share` = the
single account carrying the largest share of a cell's weight (concentration warning at ≥ 0.5); `contributors` are
the 15 heaviest. Written to data/matrix.json.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .db import connect
from .evaluate import MATURITY_DAYS
from .models import active_model
from .score import TrustLookup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "matrix.json"
ASSETS = ("BTC", "GOLD", "SPX")
HORIZONS = ("SHORT", "MEDIUM", "LONG")
NEUTRAL_BAND = 0.15   # |net| below this → NEUTRAL
MIN_WEIGHT = 0.3      # below this total weight → N/A (insufficient data)
MIN_ACCOUNTS = 3      # fewer distinct voters → N/A (one fresh vote at 0.5 × 0.7 clears MIN_WEIGHT alone)


def _label(net: float, total: float, n_accounts: int | None = None) -> str:
    if total < MIN_WEIGHT or (n_accounts is not None and n_accounts < MIN_ACCOUNTS):
        return "N/A"
    if net > NEUTRAL_BAND:
        return "BUY"
    if net < -NEUTRAL_BAND:
        return "SELL"
    return "NEUTRAL"


def build(conn: sqlite3.Connection, today: date | None = None, write: bool = True, model: str | None = None) -> dict:
    """Matrix from one model's calls, weighted by that model's trust scores."""
    model = model or active_model()
    today = today or datetime.now(timezone.utc).date()
    trust = TrustLookup(conn, model=model, as_of=today)
    schools = {r["handle"]: r["school"] for r in conn.execute("SELECT handle, school FROM accounts")}
    matrix: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "model": model, "cells": {}}

    for asset in ASSETS:
        for horizon in HORIZONS:
            window = MATURITY_DAYS[horizon]
            half_life = window / 3
            since = (today - timedelta(days=window)).isoformat()
            until = (today + timedelta(days=1)).isoformat()  # point-in-time: no calls from after `today` (Pine history)
            rows = conn.execute("""SELECT handle, direction, confidence, called_at, quote, tweet_id FROM calls
                                   WHERE model=? AND asset=? AND horizon=? AND called_at>=? AND called_at<? ORDER BY called_at DESC, id DESC""",
                                (model, asset, horizon, since, until)).fetchall()
            # one vote per account: its latest call in the window (rows are newest-first); repeats are dropped so a
            # prolific poster cannot outvote a roster of quieter ones
            per_school: dict[str, dict] = {}
            per_handle: dict[str, float] = {}
            contributors = []
            buy = sell = neutral = 0.0
            seen: set[str] = set()
            for r in rows:
                if r["handle"] in seen:
                    continue
                seen.add(r["handle"])
                age = (today - date.fromisoformat(r["called_at"][:10])).days
                # √confidence: the classifier's 0.6–0.9 range predicts outcome by only ~9 pts, so it should not
                # swing a vote's weight by 50 %
                w = (trust.get(r["handle"], asset, horizon) * math.sqrt(r["confidence"])
                     * math.exp(-math.log(2) * age / half_life))
                d = r["direction"]
                if d == "BUY":
                    buy += w
                elif d == "SELL":
                    sell += w
                else:
                    neutral += w
                s = per_school.setdefault(schools.get(r["handle"], "?"), {"buy": 0.0, "sell": 0.0, "neutral": 0.0, "n": 0})
                s["buy" if d == "BUY" else "sell" if d == "SELL" else "neutral"] += w
                s["n"] += 1
                per_handle[r["handle"]] = per_handle.get(r["handle"], 0.0) + w
                contributors.append({"handle": r["handle"], "direction": d, "date": r["called_at"][:10],
                                     "weight": round(w, 3), "quote": r["quote"], "tweet_id": r["tweet_id"]})
            total = buy + sell + neutral
            net = (buy - sell) / total if total else 0.0
            top_handle, top_w = max(per_handle.items(), key=lambda kv: kv[1]) if per_handle else (None, 0.0)
            contributors.sort(key=lambda x: -x["weight"])
            matrix["cells"][f"{asset}:{horizon}"] = {
                "asset": asset, "horizon": horizon, "label": _label(net, total, len(seen)),
                "net": round(net, 3), "buy": round(buy, 3), "sell": round(sell, 3), "neutral": round(neutral, 3),
                "n_calls": len(rows), "n_accounts": len(seen),
                "top_handle": top_handle, "top_share": round(top_w / total, 3) if total else 0.0,
                "schools": {k: {**{kk: (round(vv, 3) if kk != "n" else vv) for kk, vv in v.items()},
                                "label": _label((v["buy"] - v["sell"]) / ((v["buy"] + v["sell"] + v["neutral"]) or 1),
                                                v["buy"] + v["sell"] + v["neutral"], v["n"])}
                            for k, v in per_school.items()},
                "contributors": contributors[:15],
            }
    if write:
        OUT.write_text(json.dumps(matrix, indent=1, ensure_ascii=False))
    return matrix


def print_grid(m: dict) -> None:
    print(f"{'':6}" + "".join(f"{h:>10}" for h in HORIZONS))
    for a in ASSETS:
        print(f"{a:6}" + "".join(f"{m['cells'][f'{a}:{h}']['label']:>10}" for h in HORIZONS))


if __name__ == "__main__":
    print_grid(build(connect()))
