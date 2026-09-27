"""Generate the TradingView Pine indicator from the DB → tradingview/finclator.pine

Pine can't fetch HTTP, so the signal history is embedded as run-length-encoded constants. The matrix changes at most
weekly, so 3 years × 9 cells is a few hundred numbers. Re-run after every pipeline run; paste into Pine editor.
Each cell carries the continuous net score (−1…+1, buy−sell share of trust-weighted calls) — the label is derived in
Pine with the same ±NEUTRAL_BAND used by matrix.py, so the oscillator and the table can never disagree.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .db import connect, log
from .matrix import ASSETS, HORIZONS, NEUTRAL_BAND, build
from .models import active_model

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tradingview" / "finclator.pine"
NET_STEP = 0.05  # start a new run when the net score moves at least this much (or the label flips)
LABEL_NUM = {"BUY": 1, "NEUTRAL": 0, "SELL": -1, "N/A": 0}


def history(conn: sqlite3.Connection, start: date, end: date, step_days: int = 7,
            model: str | None = None) -> dict[str, list[tuple[str, float, int]]]:
    """Weekly matrix snapshots from start→end, as (date, net, n_calls) runs per cell. N/A cells carry net=0."""
    series: dict[str, list[tuple[str, float, int]]] = {f"{a}:{h}": [] for a in ASSETS for h in HORIZONS}
    weeks = (end - start).days // step_days + 1
    log(f"pine: history {start} → {end}, {weeks} weekly point-in-time matrices")
    d = start
    i = 0
    while d <= end:
        m = build(conn, today=d, write=False, model=model)
        for k, cell in m["cells"].items():
            net = 0.0 if cell["label"] == "N/A" else round(cell["net"], 2)
            s = series[k]
            if not s or abs(s[-1][1] - net) >= NET_STEP or LABEL_NUM[cell["label"]] != _label_num(s[-1][1]):
                s.append((d.isoformat(), net, cell["n_calls"]))
        d += timedelta(days=step_days)
        i += 1
        if i % 10 == 0 or d > end:
            log(f"pine: {i}/{weeks} weeks (at {m['cells']['BTC:SHORT']['label']}/{m['cells']['GOLD:SHORT']['label']}/"
                f"{m['cells']['SPX:SHORT']['label']} short as of {min(d, end)})")
    return series


def _label_num(net: float) -> int:
    return 1 if net > NEUTRAL_BAND else -1 if net < -NEUTRAL_BAND else 0


def _pine_arrays(runs: list[tuple[str, float, int]]) -> tuple[str, str, str]:
    ts = ", ".join(f"timestamp('{d} 00:00 +0000')" for d, _, _ in runs)
    vs = ", ".join(f"{v:.2f}" for _, v, _ in runs)
    ns = ", ".join(str(n) for _, _, n in runs)
    return f"array.from({ts})", f"array.from({vs})", f"array.from({ns})"


def render(series: dict[str, list[tuple[str, float, int]]], generated: str) -> str:
    decl = []
    for a in ASSETS:
        for h in HORIZONS:
            t, v, n = _pine_arrays(series[f"{a}:{h}"] or [("2020-01-01", 0.0, 0)])
            p = f"{a.lower()}_{h.lower()}"
            decl.append(f"var {p}_t = {t}")
            decl.append(f"var {p}_v = {v}")
            decl.append(f"var {p}_n = {n}")
    decls = "\n".join(decl)
    band = NEUTRAL_BAND
    return f'''// This source code is subject to the terms of the Mozilla Public License 2.0 at https://mozilla.org/MPL/2.0/
// © finclator — generated {generated}. Do not edit by hand; regenerate with `python -m src.pine`.
//
// Finclator: Finfluencer Sentiment Oscillator (BTC / GOLD / SPX × SHORT / MEDIUM / LONG)
// Explicit market calls extracted from a fixed roster of finance accounts on X are evaluated against price once
// their horizon matures; each account earns a track-record score, and the current calls are aggregated weighted by
// that score. net = (buy − sell) / total trust-weighted calls, in −1…+1. |net| ≤ {band} is NEUTRAL.
// History is embedded (weekly snapshots, point-in-time: each value uses only calls and outcomes known at that date).
//@version=5
indicator("Finclator: Finfluencer Sentiment Oscillator", shorttitle="Finclator", overlay=false, precision=2)

// ── embedded signal history (run-length: value applies from timestamp until the next one) ──────────────────
{decls}

f_at(tArr, vArr, t) =>
    v = 0.0
    for i = 0 to array.size(tArr) - 1
        if t >= array.get(tArr, i)
            v := array.get(vArr, i)
    v

f_n(tArr, nArr, t) =>
    v = 0
    for i = 0 to array.size(tArr) - 1
        if t >= array.get(tArr, i)
            v := array.get(nArr, i)
    v

// ── which asset is this chart? ──────────────────────────────────────────────────────────────────────────────
assetIn = input.string("auto", "asset", options=["auto", "BTC", "GOLD", "SPX"])
sym = syminfo.ticker
isBTC  = str.contains(sym, "BTC")
isGOLD = str.contains(sym, "XAU") or str.contains(sym, "GC") or str.contains(sym, "GOLD") or str.contains(sym, "GLD")
isSPX  = str.contains(sym, "SPX") or str.contains(sym, "SPY") or str.contains(sym, "ES") or str.contains(sym, "US500")
thisAsset = assetIn != "auto" ? assetIn : isBTC ? "BTC" : isGOLD ? "GOLD" : isSPX ? "SPX" : "BTC"

f_cell(asset, hz) =>
    asset == "BTC"  and hz == "S" ? f_at(btc_short_t,  btc_short_v,  time) :
    asset == "BTC"  and hz == "M" ? f_at(btc_medium_t, btc_medium_v, time) :
    asset == "BTC"  and hz == "L" ? f_at(btc_long_t,   btc_long_v,   time) :
    asset == "GOLD" and hz == "S" ? f_at(gold_short_t, gold_short_v, time) :
    asset == "GOLD" and hz == "M" ? f_at(gold_medium_t,gold_medium_v,time) :
    asset == "GOLD" and hz == "L" ? f_at(gold_long_t,  gold_long_v,  time) :
    asset == "SPX"  and hz == "S" ? f_at(spx_short_t,  spx_short_v,  time) :
    asset == "SPX"  and hz == "M" ? f_at(spx_medium_t, spx_medium_v, time) :
                                    f_at(spx_long_t,   spx_long_v,   time)

f_calls(asset, hz) =>
    asset == "BTC"  and hz == "S" ? f_n(btc_short_t,  btc_short_n,  time) :
    asset == "BTC"  and hz == "M" ? f_n(btc_medium_t, btc_medium_n, time) :
    asset == "BTC"  and hz == "L" ? f_n(btc_long_t,   btc_long_n,   time) :
    asset == "GOLD" and hz == "S" ? f_n(gold_short_t, gold_short_n, time) :
    asset == "GOLD" and hz == "M" ? f_n(gold_medium_t,gold_medium_n,time) :
    asset == "GOLD" and hz == "L" ? f_n(gold_long_t,  gold_long_n,  time) :
    asset == "SPX"  and hz == "S" ? f_n(spx_short_t,  spx_short_n,  time) :
    asset == "SPX"  and hz == "M" ? f_n(spx_medium_t, spx_medium_n, time) :
                                    f_n(spx_long_t,   spx_long_n,   time)

BAND = {band}
f_lbl(v) => v > BAND ? 1 : v < -BAND ? -1 : 0

s = f_cell(thisAsset, "S")
m = f_cell(thisAsset, "M")
l = f_cell(thisAsset, "L")
wS = input.float(1.0, "weight SHORT (0–3 mo)",   minval=0, step=0.1, group="composite")
wM = input.float(1.0, "weight MEDIUM (3–12 mo)", minval=0, step=0.1, group="composite")
wL = input.float(1.0, "weight LONG (1–5 y)",     minval=0, step=0.1, group="composite")
composite = (s * wS + m * wM + l * wL) / (wS + wM + wL)
cLbl = f_lbl(composite)
sLbl = f_lbl(s)
mLbl = f_lbl(m)
lLbl = f_lbl(l)

// ── plots (chart asset) ─────────────────────────────────────────────────────────────────────────────────────
cUp = color.new(#0a7d33, 0)
cDn = color.new(#c0392b, 0)
cFl = color.new(#8a8a8a, 0)
plot(composite, "composite", style=plot.style_columns, color=cLbl > 0 ? cUp : cLbl < 0 ? cDn : cFl)
plot(s, "SHORT net",  color=color.new(#1a5fb4, 0), linewidth=1)
plot(m, "MEDIUM net", color=color.new(#e08a00, 0), linewidth=1)
plot(l, "LONG net",   color=color.new(#7b2cbf, 0), linewidth=2)
hline(0, color=color.new(color.gray, 60))
bUp = hline(BAND,  color=color.new(color.gray, 85), linestyle=hline.style_dotted)
bDn = hline(-BAND, color=color.new(color.gray, 85), linestyle=hline.style_dotted)
fill(bUp, bDn, color=color.new(color.gray, 94), title="neutral band")
hline(1, color=color.new(color.gray, 90), linestyle=hline.style_dotted)
hline(-1, color=color.new(color.gray, 90), linestyle=hline.style_dotted)

// ── alerts (set once on the chart; fire when the roster's consensus changes) ────────────────────────────────
alertcondition(cLbl != cLbl[1], "Composite flipped", "Finclator {{{{ticker}}}}: composite sentiment changed")
alertcondition(cLbl > 0 and cLbl[1] <= 0, "Composite → BUY",  "Finclator {{{{ticker}}}}: composite turned BUY")
alertcondition(cLbl < 0 and cLbl[1] >= 0, "Composite → SELL", "Finclator {{{{ticker}}}}: composite turned SELL")
alertcondition(sLbl != sLbl[1], "SHORT flipped",  "Finclator {{{{ticker}}}}: SHORT (0–3 mo) sentiment changed")
alertcondition(mLbl != mLbl[1], "MEDIUM flipped", "Finclator {{{{ticker}}}}: MEDIUM (3–12 mo) sentiment changed")
alertcondition(lLbl != lLbl[1], "LONG flipped",   "Finclator {{{{ticker}}}}: LONG (1–5 y) sentiment changed")

// ── 3×3 table (all assets, current bar) ────────────────────────────────────────────────────────────────────
showTbl = input.bool(true, "show 3×3 table", group="table")
showN   = input.bool(true, "show call counts", group="table")
f_txt(v, n) => (v > BAND ? "BUY" : v < -BAND ? "SELL" : "NEUTRAL") + " " + (v >= 0 ? "+" : "") + str.tostring(v, "0.00") + (showN ? "\\n" + str.tostring(n) + " calls" : "")
f_alpha(v) => 100 - math.round(30 + 50 * math.min(math.abs(v), 1))
f_col(v) => v > BAND ? color.new(#0a7d33, f_alpha(v)) : v < -BAND ? color.new(#c0392b, f_alpha(v)) : color.new(#8a8a8a, 80)
var tbl = table.new(position.top_right, 4, 4, border_width=1, border_color=color.new(color.gray, 70))
if showTbl and barstate.islast
    table.cell(tbl, 0, 0, "Finclator", text_color=color.gray, text_size=size.small)
    table.cell(tbl, 1, 0, "SHORT\\n0–3 mo",   text_color=color.gray, text_size=size.small)
    table.cell(tbl, 2, 0, "MEDIUM\\n3–12 mo", text_color=color.gray, text_size=size.small)
    table.cell(tbl, 3, 0, "LONG\\n1–5 y",     text_color=color.gray, text_size=size.small)
    assets = array.from("BTC", "GOLD", "SPX")
    for r = 0 to 2
        a = array.get(assets, r)
        table.cell(tbl, 0, r + 1, a, text_color=a == thisAsset ? color.white : color.gray, bgcolor=a == thisAsset ? color.new(color.blue, 60) : na, text_size=size.small)
        vS = f_cell(a, "S")
        vM = f_cell(a, "M")
        vL = f_cell(a, "L")
        table.cell(tbl, 1, r + 1, f_txt(vS, f_calls(a, "S")), bgcolor=f_col(vS), text_color=color.white, text_size=size.small)
        table.cell(tbl, 2, r + 1, f_txt(vM, f_calls(a, "M")), bgcolor=f_col(vM), text_color=color.white, text_size=size.small)
        table.cell(tbl, 3, r + 1, f_txt(vL, f_calls(a, "L")), bgcolor=f_col(vL), text_color=color.white, text_size=size.small)
'''


def generate(conn: sqlite3.Connection, years: int = 3, model: str | None = None) -> Path:
    model = model or active_model()
    end = date.today()
    ser = history(conn, end - timedelta(days=years * 365), end, model=model)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(ser, f"{end.isoformat()} · model {model}"))
    (ROOT / "data" / "matrix_history.json").write_text(json.dumps({"model": model, "series": ser}, indent=1))
    log(f"pine: wrote {OUT.relative_to(ROOT)} + data/matrix_history.json ({sum(len(v) for v in ser.values())} runs)")
    return OUT


if __name__ == "__main__":
    print(generate(connect()))
