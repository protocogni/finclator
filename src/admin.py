"""Admin page: live progress, matrix, trust and audit — reads SQLite on every request.

    python -m src.admin            # http://127.0.0.1:8787
    python -m src.admin --port N
"""
from __future__ import annotations

import argparse
import contextvars
import html
import json
import re
import subprocess
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit, db, matrix, score
from .db import LOG_PATH as LOG
from .db import connect, log
from .gate import THRESHOLD as GATE_THRESHOLD
from .models import active_model

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ("BTC", "GOLD", "SPX")
HORIZONS = ("SHORT", "MEDIUM", "LONG")

# Per-request chrome. The hosted panel (api/panel.py) sets prefix='/panel', who=<user block>, readonly=True,
# refresh=False; the local server leaves the default. Set with CHROME.set(...) and reset in finally — never wrap
# _page: a wrapper that survives a failed request nests itself (that was the duplicated sign-out block).
CHROME: contextvars.ContextVar[dict | None] = contextvars.ContextVar("chrome", default=None)


def _chrome() -> dict:
    return CHROME.get() or {}


def _u(path: str) -> str:
    """Admin route → href honouring the hosted prefix: '/' → '/panel', '/tables' → '/panel/tables'."""
    prefix = _chrome().get("prefix", "")
    if not prefix:
        return path
    return prefix if path == "/" else prefix + path


def _readonly() -> bool:
    return bool(_chrome().get("readonly"))

CSS = """
body{font:14px system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
nav{display:flex;flex-wrap:wrap;gap:12px 18px;padding:10px 18px;background:#181b22;border-bottom:1px solid #2a2f3a;position:sticky;top:0;z-index:2}
nav a{color:#9ecbff;text-decoration:none}nav a.on{color:#fff;font-weight:600}
main{padding:18px;max-width:1500px}
h2{margin:22px 0 8px;font-size:16px}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.card{background:#181b22;border:1px solid #2a2f3a;border-radius:8px;padding:10px 14px;min-width:130px}
.card b{display:block;font-size:20px}.card small{color:#9aa}
table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #2a2f3a;padding:4px 8px;text-align:left}
th{background:#1d2129;cursor:pointer}td.num{text-align:right;font-variant-numeric:tabular-nums}
.grid td{text-align:center;font-weight:700;width:120px;height:52px}
.BUY{background:#1e6b3a}.SELL{background:#7a2323}.NEUTRAL{background:#444}.NA{background:#222;color:#888}
.bar{height:8px;background:#2a2f3a;border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:#4c9aff}
.ok{color:#7ddc8a}.warn{color:#f0b64c}.err{color:#ff6b6b}
pre{background:#0a0c10;padding:10px;border-radius:6px;max-height:340px;overflow:auto;font-size:12px}
.mono{font-family:ui-monospace,monospace}
.help{color:#9aa;font-size:12px;margin:2px 0 10px;max-width:1100px;line-height:1.45}
td.stale{color:#f0b64c}
.tw{overflow-x:auto;max-width:100%}
.fm{width:100%;max-width:980px;margin:6px 0 14px}.fm td:first-child{white-space:nowrap;font-weight:600;color:#fff}
.fm td.f{font-family:ui-monospace,monospace;font-size:12.5px;white-space:nowrap;color:#ffd479}.fm td{padding:5px 10px;vertical-align:top;line-height:1.45}
.fm td:last-child{color:#d6dae3}
@media (max-width:700px){main{padding:10px}.card{min-width:0;flex:1 1 44%}
.grid{width:100%;table-layout:fixed}.grid td{width:auto;min-width:0;height:auto;padding:5px 2px;font-size:11px;white-space:normal;overflow-wrap:anywhere}
.grid td small{font-size:9px;white-space:normal;display:block}.grid th{font-size:11px;padding:3px 2px;white-space:normal}.grid td:first-child,.grid th:first-child{width:38px}
.tw{width:100%}table{font-size:12px}th,td{padding:3px 5px}nav span[style]{display:none}
.fm td.f,.fm td:first-child{white-space:normal}}
"""

JS = """
document.addEventListener('click',e=>{const th=e.target.closest('th');if(!th||!th.closest('table.sortable'))return;
const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),tb=t.tBodies[0],asc=th.dataset.asc!=='1';
th.dataset.asc=asc?'1':'0';const v=td=>td.dataset.v!==undefined?+td.dataset.v:(isNaN(parseFloat(td.textContent))?td.textContent:parseFloat(td.textContent));
[...tb.rows].sort((a,b)=>{const x=v(a.cells[i]),y=v(b.cells[i]);return (x>y?1:x<y?-1:0)*(asc?1:-1)}).forEach(r=>tb.appendChild(r));});
async function tailLog(){const el=document.getElementById('log');if(!el)return;
 try{const t=await (await fetch(LOG_URL)).text();if(t!==el.textContent){el.textContent=t;
 if(document.getElementById('follow').checked)el.scrollTop=el.scrollHeight;}}catch(e){}}
window.addEventListener('load',()=>{const el=document.getElementById('log');if(el){el.scrollTop=el.scrollHeight;setInterval(tailLog,3000);}});
// influencer-link clicks → /api/click (which accounts people actually look at; see Progress → "most clicked")
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="https://x.com/"]');if(!a)return;
 const h=a.getAttribute('href').split('/')[3];if(!h||h==='status')return;
 const body=JSON.stringify({handle:h,src:document.body.dataset.tab||'',page:location.pathname});
 try{navigator.sendBeacon(CLICK_URL,new Blob([body],{type:'application/json'}))}catch(x){}});
"""


def _q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def _one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def _proc_running(pattern: str) -> bool:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return bool(out.strip())


_credits_cache: dict = {"t": 0.0, "v": None}


def _credits() -> float | None:
    """twitterapi.io balance in USD, cached 5 min (1 USD = 100,000 credits)."""
    import os
    import time
    import urllib.request
    if time.time() - _credits_cache["t"] < 300:
        return _credits_cache["v"]
    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key and (ROOT / ".env").exists():
        key = next((ln.split("=", 1)[1].strip() for ln in (ROOT / ".env").read_text().splitlines()
                    if ln.startswith("TWITTERAPI_IO_KEY=")), None)
    v = None
    if key:
        try:
            req = urllib.request.Request("https://api.twitterapi.io/oapi/my/info", headers={"X-API-Key": key})
            info = json.load(urllib.request.urlopen(req, timeout=10))
            v = (info.get("recharge_credits", 0) + info.get("bonus_credits", 0)) / 100_000
        except Exception:  # noqa: BLE001
            v = None
    _credits_cache.update(t=time.time(), v=v)
    return v


def _log_tail(n=200) -> str:
    """Tail of data/pipeline.log; the legacy backfill.log (no timestamps) is shown until it disappears."""
    out = []
    legacy = ROOT / "data" / "backfill.log"
    if legacy.exists():
        out += ["# backfill.log (legacy, untimestamped, mtime %s UTC)" % datetime.fromtimestamp(legacy.stat().st_mtime, timezone.utc).strftime("%H:%M:%S")]
        out += legacy.read_text(errors="replace").splitlines()[-n:]
    if LOG.exists():
        out += LOG.read_text(errors="replace").splitlines()[-n:]
    return "\n".join(out[-n:])


_TABLE_OPEN = re.compile(r"<table(?=[ >])")


def _last_log_stage() -> str | None:
    """Last pipeline-stage line (fetch/gate/classify/pine/audit/site/…) in data/pipeline.log, timestamp stripped."""
    if not LOG.exists():
        return None
    for ln in reversed(LOG.read_text(errors="replace").splitlines()[-400:]):
        body = ln[20:] if len(ln) > 20 and ln[19] == " " else ln
        if re.match(r"(fetch|gate|classify|prices|evaluate|score|matrix|pine|audit|site)\b", body):
            return ln[:16] + " " + body
    return None


def _wrap_tables(body: str) -> str:
    """Give every table a horizontal-scroll container (mobile). Idempotent on already-wrapped markup."""
    if "<div class=tw>" in body:
        return body
    return _TABLE_OPEN.sub("<div class=tw><table", body).replace("</table>", "</table></div>")


def _page(title: str, body: str, active: str) -> str:
    ch = _chrome()
    tabs = [("/", "Progress"), ("/matrix", "Matrix"), ("/accounts", "Accounts"), ("/audit", "Audit"),
            ("/architecture", "Architecture"), ("/tables", "Tables"), ("/api/status", "JSON")]
    nav = "".join(f"<a href='{_u(h)}' class='{'on' if h == active else ''}'>{t}</a>" for h, t in tabs)
    live = ch.get("refresh", True) and active not in ("/tables", "/audit", "/matrix")
    refresh = f"<meta http-equiv=refresh content={60 if active == '/' else 30}>" if live else ""
    mode = ("page 60s · log live 3s" if active == "/" else "auto-refresh 30s") if live else "no auto-refresh"
    who = ch.get("who", "")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>Finclator admin — {title}</title>"
            f"{refresh}<style>{CSS}</style><script>const LOG_URL={json.dumps(_u('/api/log'))},CLICK_URL={json.dumps(_u('/api/click'))}</script><script>{JS}</script>"
            f"<body data-tab='{html.escape(active.strip('/') or 'progress')}'>"
            f"<nav>{nav}<span style='margin-left:auto;color:#9aa'>{datetime.now(timezone.utc):%H:%M:%S} UTC · {mode}</span>{who}</nav>"
            f"<main>{_wrap_tables(body)}</main></body>")


def record_click(conn, handle: str, src: str | None, page: str | None, who: str | None) -> bool:
    """Store one influencer-link click. Handle must be on the roster (drops junk); src/page are clipped."""
    handle = (handle or "").strip().lstrip("@").lower()[:40]
    if not handle or not _one(conn, "SELECT 1 FROM accounts WHERE handle=?", handle):
        return False
    conn.execute("INSERT INTO clicks(at, handle, src, page, who) VALUES(datetime('now'), ?, ?, ?, ?)",
                 (handle, (src or "")[:40] or None, (page or "")[:120] or None, (who or "")[:120] or None))
    conn.commit()
    return True


def click_stats(conn, days: int = 30, limit: int = 15) -> dict:
    """{'days', 'total', 'clickers', 'top': [{handle, n, who}], 'by_src': {src: n}} over the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    t = _one(conn, "SELECT count(*) n, count(DISTINCT who) w FROM clicks WHERE at >= ?", since)
    top = [{"handle": r["handle"], "n": r["n"], "who": r["w"]} for r in _q(
        conn, "SELECT handle, count(*) n, count(DISTINCT who) w FROM clicks WHERE at >= ? GROUP BY handle "
              "ORDER BY n DESC, handle LIMIT ?", since, limit)]
    by_src = {r["src"] or "?": r["n"] for r in _q(
        conn, "SELECT src, count(*) n FROM clicks WHERE at >= ? GROUP BY src ORDER BY n DESC", since)}
    return {"days": days, "total": t["n"], "clickers": t["w"], "top": top, "by_src": by_src}


def status(conn) -> dict:
    model = active_model()
    f = _one(conn, """SELECT count(*) t, coalesce(sum(relevant),0) rel,
                       coalesce(sum(is_reply),0) replies, coalesce(sum(CASE WHEN text LIKE 'RT @%' THEN 1 ELSE 0 END),0) rts,
                       (SELECT count(*) FROM classified_by b JOIN tweets x ON x.id=b.tweet_id WHERE b.model=? AND x.relevant=1) cls,
                       (SELECT count(*) FROM calls WHERE model=?) calls,
                       (SELECT count(*) FROM outcomes o JOIN calls c ON c.id=o.call_id WHERE c.model=?) outs,
                       (SELECT count(*) FROM trust WHERE model=? AND asset='*' AND horizon='*') scored,
                       (SELECT count(*) FROM accounts) accounts,
                       (SELECT count(*) FROM accounts WHERE active=1) active,
                       (SELECT count(DISTINCT handle) FROM tweets) fetched FROM tweets""", model, model, model, model)
    # classification throughput for the active model over the last 10 minutes (classified_by.at is UTC)
    recent = _one(conn, "SELECT count(*) n, min(at) a FROM classified_by WHERE model=? AND at >= datetime('now','-10 minutes')", model)
    rate = None
    if recent and recent["n"] >= 20:
        span = (datetime.now(timezone.utc) - datetime.fromisoformat(recent["a"]).replace(tzinfo=timezone.utc)).total_seconds()
        rate = recent["n"] / span * 60 if span > 30 else None
    g = _one(conn, """SELECT count(*) n, coalesce(sum(CASE WHEN p_call >= ? THEN 1 ELSE 0 END),0) p, max(model) m, max(at) at
                      FROM gate""", GATE_THRESHOLD)
    pending = f["rel"] - f["cls"]
    prices = {r["asset"]: dict(r) for r in _q(conn, "SELECT asset, min(date) a, max(date) b, count(*) n FROM prices GROUP BY asset")}
    matrix_p = ROOT / "data" / "matrix.json"
    matrix = json.loads(matrix_p.read_text()) if matrix_p.exists() else {}
    return {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "models": [r[0] for r in _q(conn, "SELECT model FROM classified_by GROUP BY model")],
        "backfill_running": _proc_running("scripts/backfill.py"),
        "classify_running": _proc_running("scripts/classify_run.py"),
        "run_running": _proc_running("src.run"),
        "rebuild_running": _proc_running(r"src\.(run|pine|matrix|site|audit)|rebuild\.py"),
        "rebuild_stage": _last_log_stage(),
        "tweets": f["t"], "relevant": f["rel"], "classified": f["cls"], "pending_classification": pending,
        "classify_rate_per_min": round(rate, 1) if rate else None,
        "classify_eta_hours": round(pending / rate / 60, 2) if rate else None,
        "replies_in_db": f["replies"], "retweets_in_db": f["rts"],
        "calls": f["calls"], "outcomes": f["outs"], "accounts": f["accounts"], "accounts_active": f["active"],
        "accounts_fetched": f["fetched"], "accounts_scored": f["scored"],
        "gated": g["n"], "gate_passed": g["p"], "gate_model": g["m"], "gate_last_at": g["at"],
        "prices": prices, "matrix_generated_at": matrix.get("generated_at"), "matrix_model": matrix.get("model"),
        "matrix": {k: v.get("label") for k, v in matrix.get("cells", {}).items()},
    }


def account_rows(conn, model: str):
    """Per-account fetch/classify counts in one pass (was 6 correlated subqueries × 98 accounts ≈ 2.5 s on Neon)."""
    return _q(conn, """
        SELECT a.handle, a.school, a.sampling, a.rate_per_year, a.active, a.last_fetch_at,
               count(t.id) n, coalesce(sum(t.relevant),0) rel,
               count(b.tweet_id) cls, coalesce(sum(cc.n),0) calls,
               min(t.created_at) f, max(t.created_at) l
        FROM accounts a
        LEFT JOIN tweets t ON t.handle=a.handle
        LEFT JOIN classified_by b ON b.tweet_id=t.id AND b.model=?
        LEFT JOIN (SELECT tweet_id, count(*) n FROM calls WHERE model=? GROUP BY tweet_id) cc ON cc.tweet_id=t.id
        GROUP BY a.handle, a.school, a.sampling, a.rate_per_year, a.active, a.last_fetch_at
        ORDER BY n DESC""", model, model)


def page_progress(conn) -> str:
    s = status(conn)
    e = html.escape
    pct = lambda a, b: (100 * a / b) if b else 0  # noqa: E731
    run_state = ("<small>operator machine</small>" if _readonly() else
                 "<span class=ok>running</span>" if s["backfill_running"] else "<span class=warn>idle</span>")
    cls_state = ("<small>operator machine</small>" if _readonly() else
                 "<span class=ok>running</span>" if s["classify_running"] else "<span class=warn>idle</span>")
    eta = (f"{s['classify_rate_per_min']:.0f}/min · ETA {s['classify_eta_hours']:.1f} h" if s["classify_rate_per_min"]
           else ("no throughput in last 10 min" if s["pending_classification"] else "done"))
    cards = [
        ("backfill", run_state, "scripts/backfill.py (twitterapi.io fetch)"),
        ("classifier", cls_state, f"{e(s['model'])}<br>{eta}"),
        ("pipeline rebuild", "<small>operator machine</small>" if _readonly() else
         "<span class=ok>running</span>" if s["rebuild_running"] else "<span class=warn>idle</span>",
         "matrix → audit → pine (3 y of weekly point-in-time matrices, the slow step) → site"
         + (f"<br><span class=mono>{e(s['rebuild_stage'])}</span>" if s["rebuild_stage"] else "")),
        ("Jev gate", f"{s['gated']:,}", f"{s['gate_passed']:,} passed ({pct(s['gate_passed'], s['gated']):.0f}%) · {e(s['gate_model'] or '—')}"
                                       f"<br>last {(s['gate_last_at'] or '—')[:16]}"),
        ("accounts fetched", f"{s['accounts_fetched']} / {s['accounts']}", f"{s['accounts_active']} active"),
        ("tweets", f"{s['tweets']:,}", f"replies {s['replies_in_db']} · RTs {s['retweets_in_db']} — originals-only invariant"),
        ("asset-mentioning", f"{s['relevant']:,}", f"{pct(s['relevant'], s['tweets']):.0f}% of tweets (regex stage)"),
        ("classified by this model", f"{s['classified']:,}", f"<span class='{'warn' if s['pending_classification'] else 'ok'}'>{s['pending_classification']:,} pending</span>"),
        ("calls", f"{s['calls']:,}", f"{s['outcomes']:,} matured & evaluated"),
        ("accounts scored", f"{s['accounts_scored']}", "≥ 1 matured outcome (trust exists)"),
    ]
    cr = _credits()
    if cr is not None:
        cards.append(("twitterapi.io", f"${cr:.2f}", f"$0.15 / 1K tweets + $0.15 / 1K requests → ≈ {int(cr / 0.15 * 1000):,} tweets if every request were full"))
    B = [f"<h2>Pipeline <small>· active model {e(s['model'])} · models in DB: {e(', '.join(s['models']) or '—')}</small></h2>",
         "<p class=help>Funnel, left to right: stored originals → mention an asset (regex) → labeled by the active model → "
         "explicit calls → matured &amp; evaluated → accounts with a trust score. Everything after “asset-mentioning” is per "
         "model; the header names the active one.</p><div class=cards>"]
    for t, v, sub in cards:
        B.append(f"<div class=card><small>{t}</small><b>{v}</b><small>{sub}</small></div>")
    B.append("</div>")
    never = [r[0] for r in _q(conn, "SELECT handle FROM accounts WHERE handle NOT IN (SELECT DISTINCT handle FROM tweets) ORDER BY 1")]
    B.append(f"<h2>Fetch coverage <small>({pct(s['accounts_fetched'], s['accounts']):.0f}%)</small></h2>"
             f"<div class=bar><i style='width:{pct(s['accounts_fetched'], s['accounts']):.1f}%'></i></div>"
             f"<p class=help>never fetched: {', '.join('@' + e(h) for h in never) or '—'}</p>")
    B.append(f"<h2>Classification by {e(s['model'])} <small>({pct(s['classified'], s['relevant']):.1f}% of asset-mentioning tweets · newest first)</small></h2>"
             f"<div class=bar><i style='width:{pct(s['classified'], s['relevant']):.1f}%'></i></div>")
    cov = _one(conn, "SELECT min(x.created_at) a, max(x.created_at) b FROM classified_by y JOIN tweets x ON x.id=y.tweet_id WHERE y.model=?", s["model"])
    if cov and cov["a"]:
        B.append(f"<p><small>tweets labeled by this model span {cov['a'][:10]} → {cov['b'][:10]}; trust needs calls older than 90 d (SHORT) / 365 d (MEDIUM) / 730 d (LONG).</small></p>")

    B.append("<h2>Prices</h2><table><tr><th>asset</th><th>from</th><th>to</th><th>rows</th><th>last close</th><th>age</th></tr>")
    today = datetime.now(timezone.utc).date()
    for a in ASSETS:
        p = s["prices"].get(a)
        if not p:
            B.append(f"<tr><td>{a}</td><td colspan=5 class=err>no data</td></tr>")
            continue
        last = _one(conn, "SELECT close FROM prices WHERE asset=? ORDER BY date DESC LIMIT 1", a)[0]
        age = (today - datetime.strptime(p["b"], "%Y-%m-%d").date()).days
        cls = "ok" if age <= 4 else "warn"
        B.append(f"<tr><td>{a}</td><td>{p['a']}</td><td>{p['b']}</td><td class=num>{p['n']}</td>"
                 f"<td class=num>{last:,.2f}</td><td class={cls}>{age}d</td></tr>")
    B.append("</table>")

    B.append("<h2>Per-account fetch <small>· classified / calls are for the active model</small></h2><table class=sortable><thead><tr><th>account</th><th>school</th><th>sampling</th>"
             "<th title='measured originals per year at backfill (rate estimate)'>orig/yr</th><th title='original tweets stored'>tweets</th>"
             "<th title='passed the asset-mention prefilter'>relevant</th><th title='labeled by the active model'>classified</th><th title='calls by the active model'>calls</th><th>first</th><th>last</th><th title='fetch watermark: the next run searches from here (minus a 6 h overlap)'>fetched</th></tr></thead><tbody>")
    for r in account_rows(conn, s["model"]):
        cls = "" if r["n"] else " class=warn"
        last_age = (today - datetime.fromisoformat(r["l"]).date()).days if r["l"] else None
        stale = " class=stale" if r["active"] and last_age is not None and last_age > 30 else ""
        B.append(f"<tr><td{cls}>@{e(r['handle'])}{'' if r['active'] else ' <small>(inactive)</small>'}</td><td>{e(r['school'] or '')}</td>"
                 f"<td>{e(r['sampling'] or 'full')}</td><td class=num>{r['rate_per_year'] or ''}</td>"
                 f"<td class=num>{r['n']}</td><td class=num>{r['rel']}</td><td class=num>{r['cls']}</td><td class=num>{r['calls']}</td>"
                 f"<td>{(r['f'] or '')[:10]}</td><td{stale} title='days since newest stored tweet: {last_age}'>{(r['l'] or '')[:10]}</td>"
                 f"<td>{(r['last_fetch_at'] or '')[:16].replace('T', ' ')}</td></tr>")
    B.append("</tbody></table><p class=help>amber <i>last</i> = active account with no stored tweet in 30 d — check the fetch.</p>")

    # who gets looked at: influencer-link clicks on the panel + public site (JS beacon → /api/click → `clicks`)
    ck = click_stats(conn)
    B.append(f"<h2>Most clicked influencers <small>· last {ck['days']} d · {ck['total']} clicks"
             + (f" · {ck['clickers']} signed-in users" if ck["clickers"] else "") + "</small></h2>")
    if ck["top"]:
        B.append("<table class=sortable><thead><tr><th>account</th><th title='clicks on any x.com link for this account (profile or tweet), panel + public site'>clicks</th>"
                 "<th title='distinct signed-in panel users who clicked; anonymous site visitors are not counted here'>users</th></tr></thead><tbody>")
        for t in ck["top"]:
            B.append(f"<tr><td><a href='{_u('/accounts')}#acc-{e(t['handle'])}' style='color:#9ecbff'>@{e(t['handle'])}</a></td>"
                     f"<td class=num>{t['n']}</td><td class=num>{t['who']}</td></tr>")
        B.append("</tbody></table><p class=help>by source: " + " · ".join(f"{e(k)} {v}" for k, v in ck["by_src"].items()) +
                 ". Every @account / tweet link on every tab and on the public site reports here.</p>")
    else:
        B.append("<p class=help>No clicks recorded yet — every @account / tweet link on the panel and the public site reports here.</p>")

    if not _readonly():
        B.append("<h2>pipeline.log <small>(live, last 200 lines, UTC) · <label><input type=checkbox id=follow checked> follow</label></small></h2>"
                 f"<pre id=log>{e(_log_tail())}</pre>")
    return _page("progress", "".join(B), "/")


def page_matrix(conn) -> str:
    e = html.escape
    B = []
    matrix_p = ROOT / "data" / "matrix.json"
    m = json.loads(matrix_p.read_text()) if matrix_p.exists() else {"generated_at": "", "cells": {}}
    B.append(f"<h2>Current matrix <small>generated {e(m.get('generated_at') or '—')}</small>"
             + ("" if _readonly() else f" <a href='{_u('/matrix')}?rebuild=1' style='color:#9ecbff'>rebuild now</a>") + "</h2>")
    B.append("<table class=grid><tr><th></th><th>SHORT<br><small>0–3 mo</small></th><th>MEDIUM<br><small>3–12 mo</small></th><th>LONG<br><small>1–5 y</small></th></tr>")
    for a in ASSETS:
        tds = ""
        for h in HORIZONS:
            c = m["cells"].get(f"{a}:{h}", {"label": "N/A", "n_calls": 0, "net": 0, "buy": 0, "sell": 0, "neutral": 0})
            cls = c["label"] if c["label"] != "N/A" else "NA"
            w = c.get("buy", 0) + c.get("sell", 0) + c.get("neutral", 0)
            ts = c.get("top_share", 0) or 0
            top = (f"<br><small class='{'warn' if ts >= 0.5 else ''}'>top @{e(c.get('top_handle') or '–')} {ts:.0%}</small>"
                   if c.get("top_handle") else "")
            na = c.get("n_accounts", c["n_calls"])
            tds += (f"<td class={cls}><a href='{_u('/audit')}?asset={a}&hz={h}' style='color:inherit;text-decoration:none'>{c['label']}</a>"
                    f"<br><small>{na} accts · {c['n_calls']} calls net={c['net']:+.2f} w={w:.2f}</small>{top}</td>")
        B.append(f"<tr><th>{a}</th>{tds}</tr>")
    B.append("</table>")
    B.append("<p class=help>Each cell is the trust-weighted lean of the roster inside that horizon window, <b>one vote per account</b>. "
             "Click a cell for the calls behind it on Audit.</p><table class=fm>"
             "<tr><td>vote</td><td class=f>latest call per account in the window</td><td>earlier repeats by the same account are ignored, so a prolific "
             "poster cannot outvote quieter accounts. Window = 90 / 365 / 730 d for SHORT / MEDIUM / LONG.</td></tr>"
             "<tr><td>accts · calls</td><td class=f>votes · all calls in the window</td><td>the gap between the two is how much repetition was collapsed.</td></tr>"
             "<tr><td>w</td><td class=f>Σ trust × √confidence × 2<sup>−age/(window/3)</sup></td><td>total weight of the votes; each vote is the account's "
             "trust in that cell × √(classifier confidence) × recency (halves every third of the window).</td></tr>"
             "<tr><td>net</td><td class=f>(Σw<sub>BUY</sub> − Σw<sub>SELL</sub>) / w</td><td>−1 … +1.</td></tr>"
             f"<tr><td>label</td><td class=f>BUY &gt; +{matrix.NEUTRAL_BAND} · SELL &lt; −{matrix.NEUTRAL_BAND} · else NEUTRAL</td>"
             f"<td>N/A when w &lt; {matrix.MIN_WEIGHT} or fewer than {matrix.MIN_ACCOUNTS} accounts voted — too little evidence "
             "(the same gate applies to each school sub-label).</td></tr>"
             "<tr><td>top</td><td class=f>max vote / w</td><td>the largest single account's share; amber ≥ 50 % means one account is carrying the "
             "cell — that is its view, not a consensus.</td></tr></table>")

    B.append("<h2>Are the influencers, as a group, better than “always Buy”? <small>· same matured calls, target bonus excluded</small></h2>"
             "<table><tr><th>horizon</th><th title='matured calls of the active model, all accounts'>calls scored</th>"
             "<th title='every matured call from every account: right 1, half-right 0.5, wrong 0, averaged'>all accounts, averaged</th>"
             "<th title='the same calls, pretending each had said Buy: market up 1, flat 0.5, down 0'>“always Buy” rule</th><th>difference</th></tr>")
    hr = score.hit_rates(conn, active_model())
    for hz in HORIZONS:
        v = hr.get(hz)
        if not v:
            continue
        edge = v["rate"] - v["baseline"]
        B.append(f"<tr><td>{hz}</td><td class=num>{v['n']:,}</td><td class=num>{v['rate']:.1%}</td><td class=num>{v['baseline']:.1%}</td>"
                 f"<td class='num {'ok' if edge > 0.02 else 'err' if edge < -0.02 else 'warn'}'>{edge:+.1%}</td></tr>")
    B.append("</table><table class=fm>"
             "<tr><td>all accounts, averaged</td><td class=f>Σ result / n</td><td>every matured call from every account on the roster, scored right = 1, "
             "half-right = 0.5, wrong = 0, then averaged. This is the group, not any one account (those are on the Accounts tab).</td></tr>"
             "<tr><td>“always Buy” rule</td><td class=f>same calls, direction forced to BUY</td><td>pretend each of those calls had simply said Buy: "
             "market went up = 1, flat = 0.5, down = 0. A rule with zero thought in it — the bar to clear.</td></tr>"
             "<tr><td>difference</td><td class=f>group − “always Buy”</td><td>most of the covered period was a bull market, so a high first "
             "column is not skill; only this column says whether the group beat “just buy”. Green &gt; +2 pts, red &lt; −2 pts.</td></tr></table>")

    B.append("<h2>Contributors per cell</h2><table class=fm>"
             "<tr><td>rows</td><td class=f>15 heaviest votes</td><td>one row per account — its latest call in the window.</td></tr>"
             "<tr><td>weight</td><td class=f>trust × √confidence × recency</td><td>the vote's share of the cell; the same number the matrix sums.</td></tr>"
             "<tr><td>header</td><td class=f>label (accounts, calls) · school sub-labels</td><td>each school's own reading from its members' votes.</td></tr></table>")
    for a in ASSETS:
        for h in HORIZONS:
            c = m["cells"].get(f"{a}:{h}")
            if not c or not c.get("contributors"):
                continue
            sch = " · ".join(f"{k}: {v['label']}" for k, v in c.get("schools", {}).items())
            B.append(f"<details><summary><b>{a} {h}</b> — {c['label']} ({c.get('n_accounts', '?')} accounts, {c['n_calls']} calls) <small>{e(sch)}</small></summary><table class=sortable><thead><tr>"
                     "<th>account</th><th>direction</th><th>called</th><th>weight</th><th>quote</th><th>tweet</th></tr></thead><tbody>")
            for x in c["contributors"]:
                B.append(f"<tr><td>@{e(x['handle'])}</td><td class={x['direction']}>{x['direction']}</td><td>{x['date']}</td>"
                         f"<td class=num>{x['weight']:.3f}</td><td><small>{e(x.get('quote') or '')}</small></td>"
                         f"<td><a href='https://x.com/{e(x['handle'])}/status/{x['tweet_id']}' style='color:#9ecbff'>↗</a></td></tr>")
            B.append("</tbody></table></details>")

    B.append("<h2>Trust by school</h2><table class=sortable><thead><tr><th>school</th><th>accounts</th><th>scored</th><th>mean trust</th><th>Σn</th></tr></thead><tbody>")
    for r in _q(conn, """SELECT a.school, count(*) n_acc, count(t.score) scored, avg(t.score) mean, coalesce(sum(t.n),0) sn
                          FROM accounts a LEFT JOIN trust t ON t.handle=a.handle AND t.asset='*' AND t.horizon='*' AND t.model=?
                          GROUP BY a.school ORDER BY mean DESC""", active_model()):
        B.append(f"<tr><td>{e(r['school'] or '')}</td><td class=num>{r['n_acc']}</td><td class=num>{r['scored']}</td>"
                 f"<td class=num>{(r['mean'] or 0):.3f}</td><td class=num>{r['sn']}</td></tr>")
    B.append("</tbody></table><table class=fm>"
             "<tr><td>mean trust</td><td class=f>avg(overall score)</td><td>over the school's scored members; overall = all nine cells pooled.</td></tr>"
             "<tr><td>Σn</td><td class=f>Σ matured calls</td><td>of those members. Per-school sub-labels per cell are in the contributor headers above.</td></tr></table>")
    return _page("matrix", "".join(B), "/matrix")


def page_accounts(conn) -> str:
    """One 3×3 trust grid per account (asset × horizon) — trust is cell-level, a flat table hides that."""
    e = html.escape
    model = active_model()
    trust = {(r["handle"], r["asset"], r["horizon"]): r for r in _q(conn, "SELECT * FROM trust WHERE model=?", model)}
    # matured calls behind every cell, so the grid can be verified without leaving the page
    cell_calls: dict[tuple[str, str, str], list] = {}
    for r in _q(conn, """SELECT c.handle, c.asset, c.horizon, c.direction, c.tweet_id, c.called_at, c.quote, c.price_target,
                                o.result, o.return_pct, o.target_hit
                         FROM calls c JOIN outcomes o ON o.call_id=c.id WHERE c.model=? ORDER BY c.called_at DESC""", model):
        cell_calls.setdefault((r["handle"], r["asset"], r["horizon"]), []).append(r)
    n_calls = {r["handle"]: r["n"] for r in _q(conn, "SELECT handle, count(*) n FROM calls WHERE model=? GROUP BY handle", model)}
    accounts = _q(conn, "SELECT handle, school FROM accounts")
    HZ = ("SHORT", "MEDIUM", "LONG")

    def shade(s: float | None) -> str:
        if s is None:
            return "background:#1a1d24;color:#666"
        t = max(0.0, min(1.0, (s - 0.3) / 0.4))  # 0.3 → red, 0.5 → neutral, 0.7 → green
        r, g = int(120 * (1 - t) + 30), int(30 + 90 * t)
        return f"background:rgb({r},{g},45)"

    def cell(h, a, hz):
        r = trust.get((h, a, hz))
        if not r:
            return f"<td style='{shade(None)}' title='no matured outcomes → 0.5 prior'>–</td>"
        return (f"<td style='{shade(r['score'])}' title='n={r['n']} matured calls, hits={r['correct']:.1f} (CORRECT=1, PARTIAL=0.5, ±0.25 target)'>"
                f"<b>{r['score']:.2f}</b><br><small>{r['correct']:.1f}/{r['n']}</small></td>")

    def margin(h, a, hz):  # asset-only / horizon-only aggregates in the margins
        r = trust.get((h, a, hz))
        return f"<td class=agg>{r['score']:.2f}<br><small>n={r['n']}</small></td>" if r else "<td class=agg>–</td>"

    def calls_list(h):
        rows = []
        for a in ASSETS:
            for hz in HZ:
                for c in cell_calls.get((h, a, hz), []):
                    tgt = f" · target {c['price_target']:,.0f} {'HIT' if c['target_hit'] else 'miss'}" if c["price_target"] else ""
                    rows.append(f"<tr><td>{a} {hz}</td><td class={c['direction']}>{c['direction']}</td><td>{c['called_at'][:10]}</td>"
                                f"<td class=num>{c['return_pct']:+.1f}%</td><td class={'ok' if c['result']=='CORRECT' else 'err' if c['result']=='WRONG' else 'warn'}>{c['result']}{tgt}</td>"
                                f"<td><small>{e(c['quote'] or '')}</small></td>"
                                f"<td><a href='https://x.com/{e(h)}/status/{c['tweet_id']}' style='color:#9ecbff'>↗</a></td></tr>")
        if not rows:
            return ""
        return ("<details><summary><small>matured calls behind this grid</small></summary><table><thead><tr><th>cell</th><th>dir</th>"
                "<th>called</th><th>return</th><th>result</th><th>quote</th><th></th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></details>")

    scored = [(a, trust.get((a["handle"], "*", "*"))) for a in accounts]
    scored.sort(key=lambda x: (-(x[1]["n"] if x[1] else 0), x[0]["handle"]))

    B = [f"<h2>Trust per account × asset × horizon <small>· model {e(model)}</small></h2>",
         "<p class=help>Each account is scored separately per asset and horizon; the matrix weights a call by the score of the cell it lands in, "
         "never by one number per account. Cell = shrunk hit rate <b>(hits + 5) / (n + 10)</b> over matured calls (SHORT 90d, MEDIUM 365d, LONG 730d); "
         "hits: CORRECT=1, PARTIAL=0.5 (called a move, market flat), WRONG=0 (opposite move, or NEUTRAL and a big move), ±0.25 when a stated "
         "price target hit/missed (wrong-side and unit-error targets ignored). Rows = asset, columns = horizon; "
         "the right column pools each asset over all horizons, the bottom row pools each horizon over all assets, the corner pools "
         "everything. Click an account in the ranking to jump to and highlight its grid. Grey = no matured outcome → the 0.5 prior is used, and the matrix falls back specific → asset → overall → 0.5. "
         "Colour: red ≤0.3 · neutral 0.5 · green ≥0.7.</p>",
         "<style>.tg{display:inline-block;vertical-align:top;margin:0 18px 18px 0;background:#181b22;border:1px solid #2a2f3a;border-radius:8px;padding:10px 12px;min-width:330px}"
         ".tg table{font-size:12px}.tg td,.tg th{text-align:center;width:66px;height:40px;padding:2px 4px}.tg th{background:#1d2129;cursor:default}"
         ".tg td.agg{background:#22262f;color:#bbb}.tg .hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}"
         ".tg details{margin-top:6px}.tg details table{font-size:11px}.tg details td{text-align:left;width:auto;height:auto}"
         ".tg{scroll-margin-top:70px}.tg:target{border-color:#4c9aff;box-shadow:0 0 0 3px rgba(76,154,255,.45);background:#1c2333}"
         ".tg:target .hd b{color:#fff;text-decoration:underline}</style>"]

    B.append("<h3>Ranking <small>(overall = all cells pooled; sortable)</small></h3><table class=sortable><thead><tr><th>account</th><th>school</th>"
             "<th title='calls by this model, matured or not'>calls</th><th title='matured calls'>n</th><th title='Σ points over matured calls: CORRECT 1, PARTIAL 0.5, WRONG 0, ±0.25 target hit/miss'>points</th><th title='(points + 5) / (n + 10) — all cells pooled'>overall</th>"
             "<th title='cells with ≥1 matured outcome, of 9'>cells</th></tr></thead><tbody>")
    have = [x for x in scored if x[1]]
    prior = [x for x in scored if not x[1]]
    for a, ov in have:
        h = a["handle"]
        cells = sum(1 for x in ASSETS for hz in HZ if (h, x, hz) in trust)
        B.append(f"<tr><td><a href='#acc-{e(h)}' style='color:#9ecbff'>@{e(h)}</a></td><td>{e(a['school'] or '')}</td><td class=num>{n_calls.get(h, 0)}</td>"
                 f"<td class=num>{ov['n']}</td><td class=num>{ov['correct']:.1f}</td><td class=num data-v={ov['score']}><b>{ov['score']:.3f}</b></td>"
                 f"<td class=num>{cells}/9</td></tr>")
    B.append("</tbody></table>")
    if prior:
        B.append(f"<details><summary>{len(prior)} accounts with no matured outcome (prior 0.5)</summary><table class=sortable><thead><tr>"
                 "<th>account</th><th>school</th><th title='calls by this model, matured or not'>calls</th></tr></thead><tbody>")
        for a, _ in prior:
            h = a["handle"]
            B.append(f"<tr><td><a href='#acc-{e(h)}' style='color:#9ecbff'>@{e(h)}</a></td><td>{e(a['school'] or '')}</td><td class=num>{n_calls.get(h, 0)}</td></tr>")
        B.append("</tbody></table></details>")

    B.append("<h3>Grids <small>(accounts with matured outcomes first)</small></h3><div>")
    for a, ov in scored:
        h = a["handle"]
        ovs = f"overall <b>{ov['score']:.3f}</b> · n={ov['n']}" if ov else "<span style='color:#888'>no matured outcomes · prior 0.5</span>"
        B.append(f"<div class=tg id='acc-{e(h)}'><div class=hd><a href='https://x.com/{e(h)}' style='color:#9ecbff'><b>@{e(h)}</b></a>"
                 f"<small>{e(a['school'] or '')} · {n_calls.get(h, 0)} calls</small></div>"
                 f"<table><thead><tr><th></th><th>SHORT<br><small>0–3m</small></th><th>MEDIUM<br><small>3–12m</small></th><th>LONG<br><small>1–5y</small></th>"
                 "<th class=agg title='this asset pooled over all three horizons'><small>all<br>horizons</small></th></tr></thead><tbody>")
        for x in ASSETS:
            B.append(f"<tr><th>{x}</th>" + "".join(cell(h, x, hz) for hz in HZ) + margin(h, x, "*") + "</tr>")
        B.append("<tr><th class=agg title='this horizon pooled over all three assets'><small>all<br>assets</small></th>"
                 + "".join(margin(h, "*", hz) for hz in HZ) + f"<td class=agg title='all cells pooled'><small>{ovs}</small></td></tr>")
        B.append("</tbody></table>" + calls_list(h) + "</div>")
    B.append("</div>")
    return _page("accounts", "".join(B), "/accounts")


def page_architecture(conn) -> str:
    from .classify import BATCH_SIZE
    from .evaluate import MATURITY_DAYS, TARGET_RATIO_BY_ASSET
    from .prefilter import _ASSET_PATTERNS
    e = html.escape
    model = active_model()
    f = _one(conn, """SELECT count(*) t, coalesce(sum(relevant),0) rel,
                       (SELECT count(*) FROM classified_by b JOIN tweets x ON x.id=b.tweet_id WHERE b.model=? AND x.relevant=1) cls,
                       (SELECT count(*) FROM calls WHERE model=?) calls, (SELECT count(DISTINCT tweet_id) FROM calls WHERE model=?) ct,
                       (SELECT count(*) FROM outcomes o JOIN calls c ON c.id=o.call_id WHERE c.model=?) outs,
                       (SELECT count(*) FROM accounts) acc FROM tweets""", model, model, model, model)
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "–"  # noqa: E731
    NB, MW = matrix.NEUTRAL_BAND, matrix.MIN_WEIGHT
    B = ["<style>.doc{max-width:980px;line-height:1.55}.doc p,.doc li{color:#d6dae3}.doc h2{margin-top:30px;padding-top:10px;"
         "border-top:1px solid #2a2f3a;font-size:17px}.doc code{background:#262b36;padding:1px 5px;border-radius:4px;font-size:12.5px}"
         ".st{display:grid;grid-template-columns:110px 1fr;gap:6px 14px;background:#1c2029;border:1px solid #2e3441;border-radius:8px;"
         "padding:12px 16px;margin:8px 0 14px}.st dt{color:#9aa;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding-top:2px}"
         ".st dd{margin:0}.st dd .n{font-variant-numeric:tabular-nums;font-weight:600;color:#fff}"
         ".flow{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch;margin:8px 0 4px}.flow a{flex:1 1 150px;min-width:150px;background:#1c2029;"
         "border:1px solid #2e3441;border-radius:8px;padding:10px 12px;text-decoration:none;color:inherit}.flow a:hover{border-color:#4c9aff}"
         ".flow b{display:block;font-size:13px;color:#9ecbff}.flow .n{display:block;font-size:20px;font-weight:700;color:#fff;margin:2px 0}"
         ".flow small{color:#9aa}"
         ".doc table td,.doc table th{padding:6px 10px;vertical-align:top}.pat td.mono{white-space:normal;overflow-wrap:anywhere;font-size:11.5px;color:#c9d1e0}"
         "@media (max-width:700px){.st{grid-template-columns:1fr}}</style><div class=doc>"]

    def stat(n):
        return f"<span class=n>{n:,}</span>"

    # ── 1. Pipeline overview: one card per stage, live counts ──────────────────────────────────────────────
    B.append(f"<h2 style='border:0;margin-top:4px'>Pipeline <small>· live counts for the active model <code>{e(model)}</code></small></h2>"
             "<p class=help>Left to right, every day at 06:00: each stage only ever adds rows — nothing is re-fetched or re-labeled. "
             "Click a card to jump to its section.</p><div class=flow>"
             f"<a href='#s-fetch'><b>1 · fetch</b><span class=n>{f['t']:,}</span><small>tweets · {f['acc']} accounts · 3 y · originals only</small></a>"
             f"<a href='#s-prefilter'><b>2 · prefilter</b><span class=n>{f['rel']:,}</span><small>mention an asset · {pct(f['rel'], f['t'])} of tweets · regex, free</small></a>"
             f"<a href='#s-gate'><b>3 · gate + classify</b><span class=n>{f['calls']:,}</span><small>calls from {f['ct']:,} tweets · {pct(f['cls'], f['rel'])} of relevant labeled</small></a>"
             f"<a href='#s-eval'><b>4 · evaluate</b><span class=n>{f['outs']:,}</span><small>matured outcomes vs Yahoo closes</small></a>"
             "<a href='#s-trust'><b>5 · trust</b><span class=n>3×3</span><small>per account · asset · horizon</small></a>"
             "<a href='#s-matrix'><b>6 · matrix</b><span class=n>3×3</span><small>BUY / NEUTRAL / SELL → site, panel, Pine</small></a></div>")

    # ── 2. Stages ──────────────────────────────────────────────────────────────────────────────────────────
    B.append(f"<h2 id=s-fetch>1 · Fetch <small>src/fetch.py</small></h2><dl class=st>"
             f"<dt>source</dt><dd>twitterapi.io <code>advanced_search</code>, one exact <code>since_time</code>/<code>until_time</code> window per account "
             f"from its <code>last_fetch_at</code> watermark (6 h overlap). {f['acc']} curated accounts, 3-year backfill.</dd>"
             "<dt>rule</dt><dd><b>Originals only.</b> Replies and retweets are rejected three times: in the API query, in the response filter, "
             "and at the DB insert (so CSV imports obey it too).</dd>"
             "<dt>heavy posters</dt><dd>&gt;1,000 originals/yr are <i>sampled</i>: the asset keywords go into the search query so the vendor bills only "
             "for tweets that can matter.</dd>"
             f"<dt>output</dt><dd><code>tweets</code> — {stat(f['t'])} rows.</dd></dl>")

    B.append("<h2 id=s-prefilter>2 · Prefilter <small>src/prefilter.py · stage 1, generous</small></h2><dl class=st>"
             "<dt>question</dt><dd>“Does the tweet mention BTC, GOLD or SPX at all?” — recall over precision: a false positive costs a fraction of a "
             "cent at the next stage, a false negative is a lost call forever.</dd>"
             "<dt>how</dt><dd>Pure regex, EN + TR. Text is HTML-unescaped and URLs stripped first. Word boundaries are Turkish-aware "
             "(Python's <code>\\b</code> is ASCII-only, so <i>altının</i> would otherwise never match).</dd>"
             "<dt>ambiguity</dt><dd><i>hisse / borsa / endeks</i> alone usually mean BIST, so they count as SPX only with a US cue "
             "(ABD, Fed, Nasdaq, Tesla…) <b>and</b> no BIST cue (THY, Aselsan, xu100…).</dd>"
             f"<dt>output</dt><dd><code>tweets.relevant</code> + <code>assets_hint</code> — {stat(f['rel'])} rows ({pct(f['rel'], f['t'])}). "
             f"Spot-check the rejects on <a href='{_u('/audit')}' style='color:#9ecbff'>Audit → “prefilter dropped”</a>.</dd></dl>"
             "<details><summary><small>patterns, live from code</small></summary><table class=pat><tr><th>asset</th><th>regex</th></tr>")
    for a in ASSETS:
        B.append(f"<tr><td>{a}</td><td class=mono>{e(_ASSET_PATTERNS[a].pattern)}</td></tr>")
    B.append("</table></details>")

    B.append(f"<h2 id=s-gate>3 · Gate + classify <small>src/gate.py · src/classify.py · stage 2, strict</small></h2>"
             "<p>Two models in series answer “is this an <b>explicit, falsifiable call</b>?”. Past-move reports, news, charts without opinion "
             "and generic macro talk are <code>is_call=false</code>.</p><dl class=st>"
             "<dt>3a · gate</dt><dd>TypeSafe <b>Jev</b> decision model (typed probabilities, no text). The classifier rules are decomposed into "
             f"questions — <code>is_call</code> probability and a per-asset stance (none/up/down/neutral). Pass when <code>p_call ≥ {GATE_THRESHOLD}</code> "
             "and at least one asset has a stance. 1,000-tweet eval vs Fable 5.1: is_call recall 0.90 at this threshold; the hybrid keeps 20 % of tweets for the GPU and loses 2 pts of call recall vs ungated. ~0.24 s and ~$0.00006 per tweet. "
             "Blocked tweets are stored as non-calls. Live for tweets fetched since 2026-09-23; the backlog was labeled without it.</dd>"
             f"<dt>3b · classifier</dt><dd>Local open-weights model via Ollama (tag defined once in <code>src/models.py</code>, active <code>{e(model)}</code>), "
             f"{BATCH_SIZE} tweets per request, temperature 0, JSON output, thinking off. Per asset it returns <b>direction</b> (BUY/SELL/NEUTRAL), "
             "<b>horizon</b>, <b>confidence</b>, <b>price target</b> in USD and an <b>exact quote</b> from the tweet that justifies the label.</dd>"
             "<dt>model dimension</dt><dd>Every label carries its model in <code>calls.model</code> / <code>classified_by</code>. Models never overwrite "
             "each other; every page counts one model at a time.</dd>"
             f"<dt>output</dt><dd><code>calls</code> — {stat(f['calls'])} rows from {f['ct']:,} tweets; <code>gate</code> rows carry the probability, "
             "copied to <code>calls.gate_p</code> (Audit flags <code>low-gate</code> below 0.5).</dd></dl>"
             "<table><tr><th>horizon</th><th>meaning (spec)</th><th>evaluated after</th><th>inferred when the tweet doesn't say</th></tr>"
             f"<tr><td>SHORT</td><td>0–3 months</td><td>{MATURITY_DAYS['SHORT']} d</td><td>technical / level talk, swing</td></tr>"
             f"<tr><td>MEDIUM</td><td>3–12 months</td><td>{MATURITY_DAYS['MEDIUM']} d</td><td>default</td></tr>"
             f"<tr><td>LONG</td><td>1–5 years</td><td>{MATURITY_DAYS['LONG']} d</td><td>macro / structural / cycle thesis</td></tr></table>")

    B.append(f"<h2 id=s-eval>4 · Evaluate <small>src/prices.py · src/evaluate.py</small></h2><dl class=st>"
             "<dt>prices</dt><dd>Yahoo daily close: <code>BTC-USD</code>, <code>GC=F</code> (COMEX front month, not spot), <code>^GSPC</code>; "
             "stored in <code>prices</code> from 2020.</dd>"
             "<dt>when</dt><dd>A call matures at entry + 90 / 365 / 730 days (the table above). Entry = close on the tweet date, exit = close at maturity.</dd>"
             f"<dt>output</dt><dd><code>outcomes</code> — {stat(f['outs'])} matured calls, each with return, result and target hit/miss.</dd></dl>"
             "<table class=fm>"
             "<tr><td>return</td><td class=f>r = exit / entry − 1</td><td>signed; SELL calls are judged on −r.</td></tr>"
             "<tr><td>flat band</td><td class=f>b = 0.5 · σ · √days</td><td>σ = trailing-1-year daily volatility of that asset, so “flat” scales with the "
             "asset and the horizon — a 2 % move is noise for BTC over 90 d.</td></tr>"
             "<tr><td>result</td><td class=f>CORRECT = 1 · PARTIAL = 0.5 · WRONG = 0</td><td>CORRECT: market moved the predicted way beyond b (or a NEUTRAL "
             "call and it stayed inside). PARTIAL: predicted a move, market stayed inside ±b. WRONG: moved the opposite way beyond b — or a NEUTRAL "
             "call and the market moved beyond b either way (a “sideways” call is falsified by any big move; scoring it PARTIAL made NEUTRAL a free 0.5 floor).</td></tr>"
             "<tr><td>price target</td><td class=f>±0.25</td><td>+0.25 if any close inside the horizon touched the stated level, −0.25 if none did. "
             "A stated level is the most falsifiable claim an account makes. Ignored (no credit either way) when the target sits on the wrong side "
             "of the entry close — it would be hit by construction — or is a unit error (SPY points against SPX, gold “76”): "
             f"target / entry outside {TARGET_RATIO_BY_ASSET['BTC']} for BTC, {TARGET_RATIO_BY_ASSET['GOLD']} for GOLD / SPX.</td></tr></table>")

    B.append("<h2 id=s-trust>5 · Trust <small>src/score.py</small></h2>"
             "<p>Trust is a <b>cell-level</b> quantity: one score per (account, asset, horizon) — the "
             f"<a href='{_u('/accounts')}' style='color:#9ecbff'>Accounts</a> tab shows it as a 3×3 grid per account.</p><table class=fm>"
             "<tr><td>hits</td><td class=f>H = Σ result ± target credit</td><td>over the cell's matured calls, n of them.</td></tr>"
             "<tr><td>trust</td><td class=f>T = (H + 5) / (n + 10)</td><td>a hit rate shrunk toward the 0.5 prior with a 10-call pseudo-sample: one lucky call "
             "scores 0.55, not 1.0; a 50/100 record still outweighs it.</td></tr>"
             "<tr><td>fallback</td><td class=f>cell → asset → account → 0.5</td><td>when a cell has no matured outcome the matrix uses the account's "
             "pooled score for that asset, then its overall score, then the prior.</td></tr>"
             "<tr><td>point-in-time</td><td class=f>outcomes with exit ≤ as-of</td><td>any historical matrix (backtest, Pine history) only sees outcomes "
             "that had matured by that date — it never weights the past with knowledge of the future.</td></tr></table>")

    B.append(f"<h2 id=s-matrix>6 · Matrix <small>src/matrix.py</small></h2>"
             "<p>Nine cells: {BTC, GOLD, SPX} × {SHORT, MEDIUM, LONG}. Everyone on the roster contributes; noisy accounts are outweighed, not filtered.</p>"
             "<table class=fm>"
             "<tr><td>window</td><td class=f>called_at ≥ today − maturity</td><td>only calls inside the horizon's window count (90 / 365 / 730 d).</td></tr>"
             "<tr><td>one vote</td><td class=f>latest call per account</td><td>an account's earlier calls in the window are ignored — a prolific poster "
             "cannot outvote quieter accounts; the vote still decays with the age of that latest call.</td></tr>"
             "<tr><td>weight</td><td class=f>w = T · √confidence · 2<sup>−age / (window/3)</sup></td><td>trust × √(classifier confidence) × recency; half-life is a third "
             "of the window, so a 90-day-old SHORT call weighs an eighth. The square root because confidence 0.6 vs 0.9 predicts the outcome by only "
             "~9 pts — it should not swing a vote by 50 %.</td></tr>"
             "<tr><td>net</td><td class=f>net = (Σw<sub>BUY</sub> − Σw<sub>SELL</sub>) / Σw</td><td>−1 … +1.</td></tr>"
             f"<tr><td>label</td><td class=f>BUY if net &gt; +{NB} · SELL if net &lt; −{NB} · else NEUTRAL</td><td>N/A when Σw &lt; {MW} or fewer than "
             f"{matrix.MIN_ACCOUNTS} accounts voted (one fresh vote clears the weight floor on its own); the same gate applies to school sub-labels.</td></tr>"
             "<tr><td>concentration</td><td class=f>top_share = max w / Σw</td><td>shown amber ≥ 50 % — one account carrying half a cell is a warning, not a signal.</td></tr>"
             f"<tr><td>schools</td><td class=f>same, per school</td><td>each <code>accounts.school</code> gets its own sub-label per cell on <a href='{_u('/matrix')}' style='color:#9ecbff'>Matrix</a>.</td></tr></table>"
             "<p>Outputs: <code>data/matrix.json</code> → public <code>site.json</code>, this panel, the Audit page, the TradingView script (publishing on hold).</p>")

    # ── 3. Caveats & files ────────────────────────────────────────────────────────────────────────────────
    B.append("<h2>Known weak spots</h2><ul>"
             "<li>Stage 2 can't catch calls that name no asset (“this is the top” under a chart image).</li>"
             "<li>Horizon inference on terse Turkish tweets is the least reliable field.</li>"
             "<li>Labels are one model's reading. Production config vs a 120-tweet frontier-labeled holdout: is-call 97 %, direction 88 %, "
             "horizon 88 % — on only 15 gold calls, so treat those as rough (<code>data/tune_variants.txt</code>). Since 2026-09-23 the Jev gate "
             "decides is_call for new tweets (holdout is_call 98 %, recall 100 %); the text model only labels what passes.</li>"
             "<li>Sampled accounts (&gt;1,000 orig/yr) see ~10 % of their tweets — evenly spread, but sparse.</li>"
             "<li>The roster as a whole underperforms always-BUY at every horizon (Matrix tab, “hit rate vs baseline”).</li></ul>")

    B.append("<h2>Files</h2><table><tr><th>file</th><th>role</th></tr>"
             "<tr><td class=mono>roster.yaml</td><td>accounts, school, language</td></tr>"
             "<tr><td class=mono>src/db.py</td><td>schema in SQLite dialect, runs on SQLite locally and Postgres (Neon) hosted; log()</td></tr>"
             "<tr><td class=mono>src/fetch.py</td><td>stage 1 — twitterapi.io windows per account, keyword query for heavy posters, credit floor</td></tr>"
             "<tr><td class=mono>src/prefilter.py</td><td>stage 2 — asset-mention regex</td></tr>"
             "<tr><td class=mono>src/gate.py · classify.py</td><td>stage 3 — Jev is_call gate (table <code>gate</code>), text classifier</td></tr>"
             "<tr><td class=mono>src/prices.py · evaluate.py · score.py · matrix.py</td><td>stages 4–6 — outcomes → trust → 3×3</td></tr>"
             "<tr><td class=mono>src/audit.py · admin.py · site.py · pine.py</td><td>verification page, this panel, public site.json, TradingView script</td></tr>"
             "<tr><td class=mono>src/run.py</td><td>the pipeline: fetch → classify → prices → evaluate → score → matrix → audit → pine → site</td></tr>"
             "<tr><td class=mono>scripts/daily.sh</td><td>launchd com.finclator.daily 06:00 local: src.run → deploy site.json → commit</td></tr>"
             "<tr><td class=mono>scripts/backfill.py</td><td>parallel fetch of every account from its watermark (8 workers)</td></tr></table></div>")
    return _page("architecture", "".join(B), "/architecture")


def _render_rows(cur, limit_cell=160) -> str:
    e = html.escape
    cols = [d[0] for d in cur.description]
    out = ["<table class=sortable><thead><tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr></thead><tbody>"]
    for r in cur:
        tds = ""
        for c, v in zip(cols, r, strict=True):
            if v is None:
                tds += "<td><small>∅</small></td>"
            elif isinstance(v, (int, float)):
                tds += f"<td class=num>{v:,}</td>" if isinstance(v, int) else f"<td class=num>{v:.4g}</td>"
            else:
                s = str(v)
                cell = e(s[:limit_cell]) + ("…" if len(s) > limit_cell else "")
                if c == "tweet_id" or (c == "id" and cols and "text" in cols):
                    h = r[cols.index("handle")] if "handle" in cols else ""
                    cell = f"<a href='https://x.com/{e(h)}/status/{e(s)}' style='color:#9ecbff'>{e(s)}</a>"
                tds += f"<td title='{e(s[:800])}'>{cell}</td>"
        out.append(f"<tr>{tds}</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def page_audit(conn, qs: str) -> str:
    """Audit tab: newest 600 calls (or one account's full history) like the hosted panel; ?all=1 renders everything."""
    from urllib.parse import parse_qs
    q = parse_qs(qs)
    acc = (q.get("account") or [None])[0]
    limit = None if q.get("all") else 600
    return _page("audit", audit.body(conn, limit=limit, account=acc), "/audit")


def page_tables(conn, qs: str) -> str:
    from urllib.parse import parse_qs
    e = html.escape
    q = parse_qs(qs)
    name = q.get("t", [""])[0]
    offset = max(0, int(q.get("o", ["0"])[0] or 0))
    limit = min(500, max(1, int(q.get("n", ["50"])[0] or 50)))
    sql = q.get("sql", [""])[0].strip()
    tables = db.tables(conn)
    T = _u("/tables")

    B = ["<h2>Tables</h2><p class=help>* = primary key. The hosted panel queries Postgres, the local admin SQLite — write portable SQL "
         "(no PRAGMA, no sqlite_master; <code>CASE WHEN</code> instead of sum(bool)).</p><table><tr><th>table</th><th>rows</th><th>columns</th></tr>"]
    for t in tables:
        n = _one(conn, f"SELECT count(*) FROM {t}")[0]
        pk = db.primary_key(conn, t)
        cols = ", ".join(c + ("*" if c in pk else "") for c in db.columns(conn, t))
        B.append(f"<tr><td><a href='{T}?t={t}' style='color:#9ecbff'><b>{t}</b></a></td><td class=num>{n:,}</td><td><small>{e(cols)}</small></td></tr>")
    B.append("</table>")

    B.append("<h2>SQL <small>(read-only; SELECT / WITH / EXPLAIN only, 500 rows max)</small></h2>"
             f"<form method=get action={T}><textarea name=sql rows=3 style='width:100%;max-width:900px;background:#0a0c10;color:#e6e6e6;border:1px solid #2a2f3a;padding:6px;font-family:ui-monospace,monospace'>{e(sql)}</textarea><br>"
             "<button style='margin-top:6px'>run</button> "
             "<small style='color:#9aa'>examples: <code>SELECT handle, count(*) n FROM calls GROUP BY 1 ORDER BY 2 DESC</code> · "
             "<code>SELECT * FROM outcomes WHERE result='WRONG'</code> · <code>SELECT date, close FROM prices WHERE asset='BTC' ORDER BY date DESC LIMIT 10</code></small></form>")
    if sql:
        first = sql.lstrip("(").split(None, 1)[0].upper() if sql else ""
        if first not in ("SELECT", "WITH", "EXPLAIN") or ";" in sql.rstrip(";"):
            B.append("<p class=err>only a single SELECT / WITH / EXPLAIN statement is allowed</p>")
        else:
            try:
                ro = connect()
                if getattr(ro, "backend", "sqlite") == "postgres":
                    ro.execute("SET TRANSACTION READ ONLY")
                else:
                    ro.execute("PRAGMA query_only=1")
                cur = ro.execute(sql.rstrip(";") + (" LIMIT 500" if first == "SELECT" and " LIMIT " not in sql.upper() else ""))
                B.append("<h3>result</h3>" + _render_rows(cur))
                ro.rollback()
                ro.close()
            except Exception as ex:  # noqa: BLE001
                B.append(f"<p class=err>{e(str(ex))}</p>")

    if name in tables:
        total = _one(conn, f"SELECT count(*) FROM {name}")[0]
        order = {"tweets": "created_at DESC", "calls": "called_at DESC", "outcomes": "exit_date DESC", "prices": "date DESC",
                 "trust": "score DESC", "accounts": "handle", "classified_by": "at DESC", "gate": "at DESC"}.get(name, "1")
        cur = conn.execute(f"SELECT * FROM {name} ORDER BY {order} LIMIT ? OFFSET ?", (limit, offset))
        prev_ = f"<a href='{T}?t={name}&o={max(0, offset - limit)}&n={limit}' style='color:#9ecbff'>← prev</a>" if offset else ""
        next_ = f"<a href='{T}?t={name}&o={offset + limit}&n={limit}' style='color:#9ecbff'>next →</a>" if offset + limit < total else ""
        B.append(f"<h2>{name} <small>rows {offset + 1:,}–{min(offset + limit, total):,} of {total:,} · order {e(order)} · "
                 f"{prev_} {next_} · <a href='{T}?t={name}&o={offset}&n=200' style='color:#9ecbff'>200/page</a></small></h2>")
        B.append(_render_rows(cur))
    return _page("tables", "".join(B), "/tables")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 — quiet
        pass

    def _send(self, body: str, ctype="text/html; charset=utf-8", code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        conn = connect()
        try:
            self._route(path, qs, conn)
        except Exception:  # noqa: BLE001 — a page bug must render, not drop the connection ("page not loading")
            tb = traceback.format_exc()
            log(f"admin: {path} failed: {tb.strip().splitlines()[-1]}")
            self._send(_page("error", f"<h2>{html.escape(path)} failed</h2><pre>{html.escape(tb)}</pre>", path), code=500)
        finally:
            conn.close()

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        if path != "/api/click":
            return self._send("not found", code=404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            body = {}
        conn = connect()
        try:
            ok = record_click(conn, body.get("handle", ""), body.get("src"), body.get("page"), None)
        finally:
            conn.close()
        self._send(json.dumps({"ok": ok}), "application/json", 200 if ok else 400)

    def _route(self, path, qs, conn):
        if True:
            if path == "/":
                self._send(page_progress(conn))
            elif path == "/matrix":
                if "rebuild=1" in qs:
                    from .matrix import build as build_matrix
                    from .score import recompute
                    recompute(conn)
                    build_matrix(conn)
                    self.send_response(302)
                    self.send_header("Location", _u("/matrix"))
                    self.end_headers()
                    return
                self._send(page_matrix(conn))
            elif path == "/accounts":
                self._send(page_accounts(conn))
            elif path == "/tables":
                self._send(page_tables(conn, qs))
            elif path == "/architecture":
                self._send(page_architecture(conn))
            elif path == "/audit":
                self._send(page_audit(conn, qs))
            elif path == "/api/log":
                self._send(_log_tail(), "text/plain; charset=utf-8")
            elif path == "/api/status":
                self._send(json.dumps(status(conn), indent=1), "application/json")
            elif path == "/api/matrix":
                p = ROOT / "data" / "matrix.json"
                self._send(p.read_text() if p.exists() else "{}", "application/json")
            else:
                self._send("not found", code=404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"admin: http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
