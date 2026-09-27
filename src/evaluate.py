"""Evaluate matured calls against price history.

Horizon maturity (spec): SHORT 3 months, MEDIUM 12 months, LONG 24 months (evaluation point inside the 1-5y band).
Direction threshold ("flat" band) is volatility-scaled: K_SIGMA * sigma_daily * sqrt(days) at entry, per asset.
Result (`grade`): CORRECT = same label; PARTIAL = predicted a move, market flat; WRONG = opposite move, or a NEUTRAL
call when the market moved beyond the band.
Price targets: hit if any close within the horizon reaches the target (>= for BUY, <= for SELL). A target on the wrong
side of the entry close, or an implausible ratio to it, is ignored (`target_is_sane`) — it would be hit by construction.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .db import connect
from .prices import close_on_or_after

MATURITY_DAYS = {"SHORT": 90, "MEDIUM": 365, "LONG": 730}
K_SIGMA = 0.5  # half a standard deviation of the horizon move counts as "flat"
TARGET_RATIO = (0.2, 5.0)  # fallback band for an unknown asset
# target / entry outside this band is a unit error, not a claim: SPY 485 vs SPX 4,846 (0.10), "DOW 50,000" vs SPX
# 6,932 (7.2), gold "76" at 4,720. BTC keeps its hyperbole — "$BTC = $1mm" at 60k (16×) is a real claim that missed.
TARGET_RATIO_BY_ASSET = {"BTC": (0.1, 100.0), "GOLD": (0.33, 3.0), "SPX": (0.33, 3.0)}


def target_is_sane(direction: str, target: float, entry_close: float, asset: str | None = None) -> bool:
    """A price target only counts when it lies on the predicted side of the entry and within a plausible ratio.

    A BUY target below entry (or SELL above) is hit by construction — max(close) ≥ entry ≥ target — and was handing
    +0.25 trust to mislabeled levels ("hold above $60k" stored as a target) and to unit errors.
    """
    if not entry_close or target <= 0:
        return False
    ratio = target / entry_close
    lo, hi = TARGET_RATIO_BY_ASSET.get(asset or "", TARGET_RATIO)
    if not (lo <= ratio <= hi):
        return False
    return ratio > 1 if direction == "BUY" else ratio < 1


def grade(pred: str, actual: str) -> str:
    """CORRECT: same label. PARTIAL: predicted a move and the market stayed flat. WRONG: the opposite move — and a
    NEUTRAL call when the market moved beyond the band either way (a "sideways" call is falsified by any big move;
    scoring it PARTIAL made NEUTRAL a free 0.5 floor that pulled trust above the prior)."""
    if pred == actual:
        return "CORRECT"
    if actual == "NEUTRAL":
        return "PARTIAL"
    return "WRONG"


def _daily_sigma(conn: sqlite3.Connection, asset: str, entry: str, lookback_days: int = 365) -> float:
    start = (date.fromisoformat(entry) - timedelta(days=lookback_days)).isoformat()
    closes = [r["close"] for r in conn.execute(
        "SELECT close FROM prices WHERE asset=? AND date>=? AND date<=? ORDER BY date", (asset, start, entry))]
    if len(closes) < 30:
        return {"BTC": 0.035, "GOLD": 0.01, "SPX": 0.011}[asset]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


def _trading_days(asset: str, days: int) -> int:
    return days if asset == "BTC" else round(days * 252 / 365)


def _extreme(conn: sqlite3.Connection, asset: str, start: str, end: str, direction: str) -> float | None:
    fn = "max" if direction == "BUY" else "min"
    return conn.execute(f"SELECT {fn}(close) FROM prices WHERE asset=? AND date>? AND date<=?",
                        (asset, start, end)).fetchone()[0]


def evaluate(conn: sqlite3.Connection, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    rows = conn.execute("""
        SELECT c.id, c.asset, c.direction, c.horizon, c.called_at, c.price_target
        FROM calls c LEFT JOIN outcomes o ON o.call_id = c.id
        WHERE o.call_id IS NULL AND c.horizon IN ('SHORT','MEDIUM','LONG')""").fetchall()
    n = 0
    for c in rows:
        entry_d = c["called_at"][:10]
        exit_d = (date.fromisoformat(entry_d) + timedelta(days=MATURITY_DAYS[c["horizon"]])).isoformat()
        if date.fromisoformat(exit_d) > today:
            continue
        entry = close_on_or_after(conn, c["asset"], entry_d)
        exit_ = close_on_or_after(conn, c["asset"], exit_d)
        if not entry or not exit_:
            continue
        ret = (exit_[1] - entry[1]) / entry[1] * 100
        sigma = _daily_sigma(conn, c["asset"], entry[0])
        thr = K_SIGMA * sigma * math.sqrt(_trading_days(c["asset"], MATURITY_DAYS[c["horizon"]])) * 100
        actual = "NEUTRAL" if abs(ret) < thr else ("BUY" if ret > 0 else "SELL")
        pred = c["direction"]
        result = grade(pred, actual)

        target_hit, extreme = None, None
        if c["price_target"] and pred in ("BUY", "SELL") and target_is_sane(pred, c["price_target"], entry[1], c["asset"]):
            extreme = _extreme(conn, c["asset"], entry[0], exit_[0], pred)
            if extreme is not None:
                target_hit = int(extreme >= c["price_target"]) if pred == "BUY" else int(extreme <= c["price_target"])

        conn.execute("""INSERT INTO outcomes(call_id, entry_date, exit_date, entry_close, exit_close, return_pct,
                        threshold_pct, actual, result, target_hit, extreme, evaluated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (c["id"], entry[0], exit_[0], entry[1], exit_[1], ret, thr, actual, result, target_hit, extreme,
                      datetime.now(timezone.utc).isoformat()))
        n += 1
    conn.commit()
    return n


if __name__ == "__main__":
    conn = connect()
    print(evaluate(conn), "outcomes written")
    for r in conn.execute("""SELECT c.handle, c.asset, c.horizon, o.result, count(*) FROM outcomes o JOIN calls c ON c.id=o.call_id
                             GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"""):
        print(tuple(r))
