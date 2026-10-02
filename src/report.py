"""Daily operator report for finclator — one email: is the system healthy, what changed, what the matrix says.

Runs as a Vercel cron (`/panel/report`, bearer CRON_SECRET, see api/panel.py) against the same Neon DB the daily
pipeline writes to, plus the bundled data/matrix.json + public/site.json (what the live site shows). Sections, in
order: verdict → problems (only if any) → pipeline freshness → 24 h activity → matrix (with label changes since the
last report) → trust / hit rates → audience (Vercel Web Analytics) → cost → watch list.

Pure functions over a `conn` so tests run on the conftest SQLite; network reads (Vercel analytics, twitterapi.io
balance, Resend) are isolated and each fails soft into a "n/a" line rather than sinking the report.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import score
from .db import connect, log
from .gate import THRESHOLD as GATE_THRESHOLD
from .models import active_model

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ("BTC", "GOLD", "SPX")
HORIZONS = ("SHORT", "MEDIUM", "LONG")

# Alarm thresholds (hours unless noted). The daily pipeline runs 06:00 America/New_York and takes ~1 h.
MATRIX_STALE_H = 30          # matrix.json older than this → the daily job did not run / deploy
FETCH_STALE_H = 48           # newest stored tweet older than this → fetch broken (roster posts daily)
PRICE_STALE_D = 5            # newest close older than this (weekends are 2–3 d)
PENDING_MAX = 300            # relevant tweets waiting for the classifier
TOP_SHARE_MAX = 0.5          # one account carrying ≥ half a cell's weight
CREDITS_MIN_USD = 2.0        # twitterapi.io balance floor (≈ 1 week of daily fetches)


# ── time helpers (DB timestamps are mixed: ISO 'T…+00:00' and 'YYYY-MM-DD HH:MM:SS' UTC) ──────────────────────────
def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def age_h(s: str | None, now: datetime) -> float | None:
    d = parse_ts(s)
    return None if d is None else round((now - d).total_seconds() / 3600, 1)


def fmt_age(h: float | None) -> str:
    if h is None:
        return "never"
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.1f} d"


# ── state (previous run) lives in the DB so label deltas survive redeploys: newest row of `reports` ────────────────
def load_state(conn) -> dict:
    r = conn.execute("SELECT labels, at FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return {}
    try:
        return {"labels": json.loads(r[0]), "at": r[1]}
    except (ValueError, TypeError):
        return {}


def save_report(conn, now: datetime, subject: str, recips: list[str], sent, problems: list, labels: dict,
                html: str) -> int:
    conn.execute("INSERT INTO reports(at, subject, recipients, sent, problems, labels, html) VALUES (?,?,?,?,?,?,?)",
                 (now.strftime("%Y-%m-%d %H:%M:%S"), subject, json.dumps(recips), json.dumps(sent, default=str),
                  json.dumps(problems, default=str), json.dumps(labels), html))
    conn.commit()
    return conn.execute("SELECT max(id) FROM reports").fetchone()[0]


# ── data ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def gather(conn, now: datetime | None = None, model: str | None = None, window_h: int = 24) -> dict:
    """Everything the email needs, DB-only (no network). Keys are stable so tests and the renderer agree."""
    now = now or datetime.now(timezone.utc)
    model = model or active_model()
    since_iso = (now - timedelta(hours=window_h)).strftime("%Y-%m-%dT%H:%M:%S")
    since_sp = since_iso.replace("T", " ")
    prev_iso = (now - timedelta(hours=2 * window_h)).strftime("%Y-%m-%dT%H:%M:%S")
    prev_sp = prev_iso.replace("T", " ")

    f = _one(conn, """SELECT count(*) t, coalesce(sum(relevant),0) rel,
                        coalesce(sum(is_reply),0) replies,
                        coalesce(sum(CASE WHEN text LIKE 'RT @%' THEN 1 ELSE 0 END),0) rts,
                        (SELECT count(*) FROM classified_by b JOIN tweets x ON x.tweet_id=b.tweet_id WHERE b.model=? AND x.relevant=1) cls,
                        (SELECT count(*) FROM calls WHERE model=?) calls,
                        (SELECT count(*) FROM outcomes o JOIN calls c ON c.id=o.call_id WHERE c.model=?) outs,
                        (SELECT count(*) FROM trust WHERE model=? AND asset='*' AND horizon='*') scored,
                        (SELECT count(*) FROM accounts) accounts,
                        (SELECT count(*) FROM accounts WHERE active=1) active
                      FROM tweets""", model, model, model, model)
    latest = {
        "tweet": _one(conn, "SELECT max(created_at) FROM tweets")[0],
        "fetch": _one(conn, "SELECT max(last_fetch_at) FROM accounts")[0],
        "gate": _one(conn, "SELECT max(at) FROM gate")[0],
        "classified": _one(conn, "SELECT max(at) FROM classified_by WHERE model=?", model)[0],
        "outcome": _one(conn, "SELECT max(evaluated_at) FROM outcomes")[0],
        "trust": _one(conn, "SELECT max(computed_at) FROM trust WHERE model=?", model)[0],
        "call": _one(conn, "SELECT max(called_at) FROM calls WHERE model=?", model)[0],
    }

    # today vs the 24 h before; each query is parametrised on its own column's timestamp format
    act = {}
    for key, sql, col, s_now, s_prev in (
        ("tweets", "SELECT count(*) FROM tweets WHERE created_at >= ?", "created_at", since_iso, prev_iso),
        ("gated", "SELECT count(*) FROM gate WHERE tweet_id IN (SELECT tweet_id FROM tweets WHERE relevant=1) "
                  "AND at >= ?", "at", since_sp, prev_sp),
        ("classified", "SELECT count(*) FROM classified_by WHERE model=? AND at >= ?", "at", since_sp, prev_sp),
        ("calls", "SELECT count(*) FROM calls WHERE model=? AND called_at >= ?", "called_at", since_iso, prev_iso),
        ("outcomes", "SELECT count(*) FROM outcomes WHERE evaluated_at >= ?", "evaluated_at", since_iso, prev_iso),
    ):
        m = (model,) if "model=?" in sql else ()
        cur = int(_one(conn, sql, *m, s_now)[0] or 0)
        prev = int(_one(conn, sql.replace(">= ?", f">= ? AND {col} < ?"), *m, s_prev, s_now)[0] or 0)
        act[key] = {"now": cur, "prev": prev}
    act["gate_passed"] = int(_one(conn, "SELECT count(*) FROM gate WHERE tweet_id IN (SELECT tweet_id FROM tweets "
                                  "WHERE relevant=1) AND at >= ? AND p_call >= ?", since_sp, GATE_THRESHOLD)[0] or 0)

    prices = {}
    for a in ASSETS:
        r = _one(conn, "SELECT date, close FROM prices WHERE asset=? ORDER BY date DESC LIMIT 1", a)
        prices[a] = {"date": r["date"], "close": float(r["close"])} if r else None

    matrix_p = ROOT / "data" / "matrix.json"
    matrix = json.loads(matrix_p.read_text()) if matrix_p.exists() else {}
    site_p = ROOT / "public" / "site.json"
    site = json.loads(site_p.read_text()) if site_p.exists() else {}
    cells = {k: {"label": v.get("label"), "net": v.get("net"), "n_accounts": v.get("n_accounts"), "n_calls": v.get("n_calls"),
                 "top_handle": v.get("top_handle"), "top_share": v.get("top_share")}
             for k, v in matrix.get("cells", {}).items()}

    stale = [r["handle"] for r in conn.execute(
        "SELECT a.handle, max(t.created_at) l FROM accounts a LEFT JOIN tweets t ON t.handle=a.handle "
        "WHERE a.active=1 GROUP BY a.handle HAVING max(t.created_at) IS NULL OR max(t.created_at) < ?",
        ((now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()]

    top_trust = [dict(handle=r["handle"], n=r["n"], score=round(float(r["score"]), 2)) for r in conn.execute(
        "SELECT handle, n, score FROM trust WHERE model=? AND asset='*' AND horizon='*' AND n >= 10 ORDER BY score DESC LIMIT 5",
        (model,)).fetchall()]
    bottom_trust = [dict(handle=r["handle"], n=r["n"], score=round(float(r["score"]), 2)) for r in conn.execute(
        "SELECT handle, n, score FROM trust WHERE model=? AND asset='*' AND horizon='*' AND n >= 10 ORDER BY score ASC LIMIT 5",
        (model,)).fetchall()]

    # who looks at whom: influencer-link clicks (panel, attributed by email) + public site (anonymous), last 7 / 30 d
    from .admin import click_stats
    clicks = {"d7": click_stats(conn, days=7, limit=8), "d30": click_stats(conn, days=30, limit=8)}
    clickers = [dict(who=r["who"], n=r["n"], handles=r["h"]) for r in conn.execute(
        "SELECT who, count(*) n, count(DISTINCT handle) h FROM clicks WHERE at >= ? AND who IS NOT NULL GROUP BY who ORDER BY n DESC LIMIT 8",
        ((now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()]

    return {
        "now": now.isoformat(timespec="seconds"), "model": model, "window_h": window_h,
        "funnel": {"tweets": f["t"], "relevant": f["rel"], "classified": f["cls"], "pending": f["rel"] - f["cls"],
                   "calls": f["calls"], "outcomes": f["outs"], "accounts": f["accounts"], "active": f["active"],
                   "scored": f["scored"], "replies": f["replies"], "rts": f["rts"]},
        "latest": latest,
        "ages_h": {k: age_h(v, now) for k, v in latest.items()},
        "activity": act,
        "prices": prices,
        "matrix": {"generated_at": matrix.get("generated_at"), "model": matrix.get("model"),
                   "age_h": age_h(matrix.get("generated_at"), now), "cells": cells},
        "site": {"generated_at": site.get("generated_at"), "age_h": age_h(site.get("generated_at"), now),
                 "calls": site.get("calls"), "outcomes": site.get("outcomes"), "scored_accounts": site.get("scored_accounts")},
        "hit_rates": score.hit_rates(conn, model),
        "stale_accounts": stale,
        "top_trust": top_trust,
        "bottom_trust": bottom_trust,
        "clicks": clicks,
        "clickers": clickers,
    }


# ── external reads (each fails soft) ─────────────────────────────────────────────────────────────────────────────
def _http_json(url: str, headers: dict, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "finclator-report/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        log(f"report: {url.split('?')[0]} failed: {e}")
        return None


def vercel_analytics(now: datetime) -> dict | None:
    """Visitors / pageviews (24 h and 7 d) + top countries from Vercel Web Analytics. Needs VERCEL_API_TOKEN."""
    tok, pid, tid = os.environ.get("VERCEL_API_TOKEN"), os.environ.get("VERCEL_PROJECT_ID"), os.environ.get("VERCEL_TEAM_ID")
    if not (tok and pid and tid):
        return None
    H = {"Authorization": f"Bearer {tok}"}
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    base = "https://api.vercel.com/v1/query/web-analytics"
    out = {}
    for name, days in (("d1", 1), ("d7", 7)):
        r = _http_json(f"{base}/visits/count?projectId={pid}&teamId={tid}&since={iso(now - timedelta(days=days))}&until={iso(now)}", H)
        out[name] = (r or {}).get("data") or {"visitors": None, "pageviews": None}
    r = _http_json(f"{base}/visits/aggregate?projectId={pid}&teamId={tid}&since={iso(now - timedelta(days=7))}&until={iso(now)}&by=country&limit=5", H)
    out["countries"] = [(x.get("country") or "?", x.get("visitors", 0)) for x in (r or {}).get("data", [])]
    r = _http_json(f"{base}/visits/aggregate?projectId={pid}&teamId={tid}&since={iso(now - timedelta(days=7))}&until={iso(now)}&by=requestPath&limit=6", H)
    out["paths"] = [(x.get("requestPath") or "?", x.get("pageviews", 0)) for x in (r or {}).get("data", [])]
    r = _http_json(f"https://api.vercel.com/v6/deployments?projectId={pid}&teamId={tid}&target=production&limit=1", H)
    d = ((r or {}).get("deployments") or [None])[0]
    out["deploy"] = {"state": d["state"], "age_h": round((now.timestamp() - d["created"] / 1000) / 3600, 1),
                     "msg": (d.get("meta", {}).get("githubCommitMessage") or "")[:60]} if d else None
    return out


def vendor_credits() -> float | None:
    """twitterapi.io balance in USD (1 USD = 100,000 credits)."""
    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        return None
    info = _http_json("https://api.twitterapi.io/oapi/my/info", {"X-API-Key": key}, timeout=10)
    return None if not info else (info.get("recharge_credits", 0) + info.get("bonus_credits", 0)) / 100_000


# ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────────────
def problems(d: dict, ext: dict | None, credits: float | None, now: datetime) -> list[tuple[str, str]]:
    """[(severity 'red'|'amber', text)] — every rule names the number it tripped on."""
    P: list[tuple[str, str]] = []
    fn, ag, mx = d["funnel"], d["ages_h"], d["matrix"]
    if mx["age_h"] is None or mx["age_h"] > MATRIX_STALE_H:
        P.append(("red", f"matrix.json is {fmt_age(mx['age_h'])} old — the 06:00 daily run did not finish or did not deploy (data/daily.log on the Mac)"))
    if mx["model"] and mx["model"] != d["model"]:
        P.append(("red", f"matrix built by {mx['model']} but the active model is {d['model']} — the site publishes a different model than the pipeline"))
    if ag["tweet"] is None or ag["tweet"] > FETCH_STALE_H:
        P.append(("red", f"newest stored tweet is {fmt_age(ag['tweet'])} old — fetch is not landing rows (twitterapi.io key / balance / watermark)"))
    if d["activity"]["tweets"]["now"] == 0 and ag["tweet"] is not None:
        P.append(("amber", f"0 tweets stored in the last {d['window_h']} h (prev window {d['activity']['tweets']['prev']})"))
    if fn["pending"] > PENDING_MAX:
        P.append(("amber", f"{fn['pending']:,} relevant tweets pending classification (limit {PENDING_MAX}) — Ollama down or the classify stage skipped"))
    if fn["replies"] or fn["rts"]:
        P.append(("red", f"originals-only invariant broken: {fn['replies']} replies / {fn['rts']} RTs in tweets"))
    today = now.date()
    for a, p in d["prices"].items():
        if not p:
            P.append(("red", f"no price rows for {a}"))
        elif (today - date.fromisoformat(p["date"])).days > PRICE_STALE_D:
            P.append(("amber", f"{a} last close is {p['date']} ({(today - date.fromisoformat(p['date'])).days} d old) — Yahoo fetch failing"))
    for k, c in mx["cells"].items():
        if c.get("top_share") and c["top_share"] >= TOP_SHARE_MAX:
            P.append(("amber", f"{k}: @{c['top_handle']} carries {c['top_share']:.0%} of the cell weight"))
    site_age, m_age = d["site"]["age_h"], mx["age_h"]
    if site_age is not None and m_age is not None and abs(site_age - m_age) > 3:
        P.append(("amber", f"public/site.json ({fmt_age(site_age)}) and matrix.json ({fmt_age(m_age)}) are from different runs — landing numbers lag the matrix"))
    if credits is not None and credits < CREDITS_MIN_USD:
        P.append(("red" if credits < 0.5 else "amber", f"twitterapi.io balance ${credits:.2f} (floor ${CREDITS_MIN_USD:.0f}) — top up or the fetch aborts"))
    if ext and ext.get("deploy") and ext["deploy"]["state"] != "READY":
        P.append(("red", f"latest production deployment is {ext['deploy']['state']}"))
    if len(d["stale_accounts"]) > 15:
        P.append(("amber", f"{len(d['stale_accounts'])} active accounts have no stored tweet in 30 d"))
    return P


def verdict(P: list[tuple[str, str]]) -> tuple[str, str]:
    if any(s == "red" for s, _ in P):
        return "🔴", "action needed"
    if P:
        return "🟡", "watch"
    return "🟢", "healthy"


# ── render ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _delta(cur: int, prev: int) -> str:
    if prev == 0 and cur == 0:
        return ""
    if prev == 0:
        return " <span style='color:#8a8073'>(new)</span>"
    ch = (cur - prev) / prev
    col = "#176c33" if ch >= 0 else "#b3261e"
    return f" <span style='color:{col};font-size:11px'>{'▲' if ch >= 0 else '▼'} {abs(ch):.0%}</span>"


def _card(label: str, value: str, sub: str = "") -> str:
    return (f"<td width='50%' style='padding:5px'><div style='background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:10px 12px'>"
            f"<div style='color:#8a8073;font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700'>{_esc(label)}</div>"
            f"<div style='font-size:20px;font-weight:800;color:#211c16;margin-top:2px'>{value}</div>"
            f"<div style='color:#8a8073;font-size:11.5px;margin-top:2px'>{sub}</div></div></td>")


def _grid(cards: list[str]) -> str:
    rows = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2] + ([""] if len(cards[i:i + 2]) == 1 else [])
        rows.append("<tr>" + "".join(p or "<td width='50%'></td>" for p in pair) + "</tr>")
    return "<table role=presentation width=100% cellpadding=0 cellspacing=0>" + "".join(rows) + "</table>"


def _h2(t: str) -> str:
    return f"<h2 style='font-size:14px;margin:22px 0 8px;color:#211c16'>{t}</h2>"


_LABEL_BG = {"BUY": "#1e6b3a", "SELL": "#7a2323", "NEUTRAL": "#555", "N/A": "#333"}


def render(d: dict, ext: dict | None, credits: float | None, P: list[tuple[str, str]], prev: dict, now: datetime) -> tuple[str, str]:
    """(subject, html)."""
    icon, word = verdict(P)
    fn, ag, act, mx = d["funnel"], d["ages_h"], d["activity"], d["matrix"]
    date_line = now.astimezone(timezone(timedelta(hours=-4))).strftime("%A, %B %-d, %Y")
    subject = f"{icon} finclator daily · {word} · {act['tweets']['now']} tweets · {act['calls']['now']} calls"

    B = [f"<!doctype html><html><head><meta charset=utf-8></head><body style='margin:0;padding:0;background:#fbf7f0'>"
         f"<div style='display:none;max-height:0;overflow:hidden'>{_esc(word)} · matrix {fmt_age(mx['age_h'])} old · {fn['pending']} pending</div>"
         f"<table role=presentation width=100% cellpadding=0 cellspacing=0 style='background:#fbf7f0;padding:22px 10px'><tr><td align=center>"
         f"<table role=presentation width=100% cellpadding=0 cellspacing=0 style='max-width:640px;font-family:-apple-system,Segoe UI,Roboto,sans-serif'><tr><td>"
         f"<div style='font-size:19px;font-weight:800;color:#211c16;letter-spacing:-.02em'>{icon} finclator — daily operations</div>"
         f"<div style='color:#8a8073;font-size:12.5px;margin:2px 0 10px'>{date_line} · last {d['window_h']} h · model <span style='font-family:ui-monospace,monospace'>{_esc(d['model'])}</span></div>"]

    if P:
        B.append(_h2("⚠️ Problems"))
        B.append("<ul style='margin:0;padding-left:18px;font-size:13px;line-height:1.5'>")
        for sev, txt in P:
            col = "#b3261e" if sev == "red" else "#8a5a00"
            B.append(f"<li style='color:{col}'>{_esc(txt)}</li>")
        B.append("</ul>")

    # freshness
    B.append(_h2("⏱ Pipeline freshness"))
    rows = [("matrix.json (deployed)", mx["age_h"], MATRIX_STALE_H), ("site.json (landing numbers)", d["site"]["age_h"], MATRIX_STALE_H),
            ("newest stored tweet", ag["tweet"], FETCH_STALE_H), ("last fetch watermark", ag["fetch"], MATRIX_STALE_H),
            ("last Jev gate", ag["gate"], None), ("last classification", ag["classified"], None),
            ("last outcome evaluated", ag["outcome"], MATRIX_STALE_H), ("trust recomputed", ag["trust"], MATRIX_STALE_H)]
    if ext and ext.get("deploy"):
        rows.append((f"prod deploy · {_esc(ext['deploy']['msg'])}", ext["deploy"]["age_h"], MATRIX_STALE_H))
    B.append("<table role=presentation width=100% cellpadding=0 cellspacing=0 style='font-size:12.5px;background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:6px 12px'>")
    for label, h, lim in rows:
        col = "#8a8073" if lim is None or h is None else ("#b3261e" if h > lim else "#176c33")
        B.append(f"<tr><td style='padding:3px 0;color:#5d554b'>{label}</td><td align=right style='padding:3px 0;color:{col};font-weight:600'>{fmt_age(h)}</td></tr>")
    B.append("</table>")
    B.append("<div style='color:#8a8073;font-size:11px;margin-top:4px'>green = inside the alarm window · the pipeline runs 06:00 ET on the operator Mac, deploys, then this report fires 08:30 ET</div>")

    # activity
    B.append(_h2(f"📥 Last {d['window_h']} h"))
    B.append(_grid([
        _card("tweets fetched", f"{act['tweets']['now']:,}{_delta(act['tweets']['now'], act['tweets']['prev'])}", "originals stored"),
        _card("Jev gate", f"{act['gated']['now']:,}", f"{act['gate_passed']:,} passed → text model"),
        _card("classified", f"{act['classified']['now']:,}{_delta(act['classified']['now'], act['classified']['prev'])}", f"{fn['pending']:,} pending"),
        _card("new calls", f"{act['calls']['now']:,}{_delta(act['calls']['now'], act['calls']['prev'])}", "explicit directional calls"),
        _card("outcomes matured", f"{act['outcomes']['now']:,}{_delta(act['outcomes']['now'], act['outcomes']['prev'])}", "evaluated against price"),
        _card("prices", " · ".join(f"{a} {p['close']:,.0f}" for a, p in d["prices"].items() if p) or "—",
              " · ".join(f"{a} {p['date'][5:]}" for a, p in d["prices"].items() if p)),
    ]))

    # matrix
    B.append(_h2("🧭 Matrix (published)"))
    prev_cells = prev.get("labels", {})
    B.append("<table cellpadding=0 cellspacing=0 style='border-collapse:separate;border-spacing:3px;font-size:12px;width:100%'>"
             "<tr><td></td>" + "".join(f"<td align=center style='color:#8a8073;font-size:11px'>{h}</td>" for h in HORIZONS) + "</tr>")
    changes = []
    for a in ASSETS:
        B.append(f"<tr><td style='color:#8a8073;font-size:11px;font-weight:700'>{a}</td>")
        for h in HORIZONS:
            c = mx["cells"].get(f"{a}:{h}", {})
            lab = c.get("label") or "N/A"
            old = prev_cells.get(f"{a}:{h}")
            chg = f"<div style='font-size:10px;opacity:.85'>was {old}</div>" if old and old != lab else ""
            if old and old != lab:
                changes.append(f"{a} {h}: {old} → {lab}")
            net = f"{c['net']:+.2f}" if c.get("net") is not None else ""
            B.append(f"<td align=center style='background:{_LABEL_BG.get(lab, '#333')};color:#fff;border-radius:6px;padding:8px 4px;font-weight:700'>"
                     f"{lab}<div style='font-size:10px;font-weight:400;opacity:.85'>{net} · {c.get('n_accounts') or 0} accts</div>{chg}</td>")
        B.append("</tr>")
    B.append("</table>")
    B.append("<div style='font-size:12px;margin-top:6px;color:#5d554b'>" + (
        "Label changes since last report: <b>" + "; ".join(changes) + "</b>" if changes else
        ("No label changes since last report." if prev_cells else "First report — no previous labels to compare.")) + "</div>")
    B.append(f"<div style='color:#8a8073;font-size:11px'>net = trust-weighted buy − sell share · accts = distinct voters (one vote per account per cell) · generated {_esc((mx['generated_at'] or '—')[:16])} UTC</div>")

    # trust / skill
    B.append(_h2("🎯 Are the influencers, as a group, better than “always Buy”?"))
    hr = d["hit_rates"]
    edges = {h: hr[h]["rate"] - hr[h]["baseline"] for h in HORIZONS if h in hr}
    if edges:
        worst = min(edges.values())
        best = max(edges.values())
        if best <= 0:
            verdict_line = (f"<b>No.</b> Averaged over all ~{fn['accounts']} accounts, their calls were right less often than a rule that "
                            f"just says “Buy” every time on the same calls (by {abs(worst):.0%} to {abs(best):.0%} points). "
                            "Most of the period was a bull market, so “Buy” was the easy answer. The matrix tells you what these accounts "
                            "currently think, not that they are right — a flip away from Buy is the informative event.")
        elif worst >= 0:
            verdict_line = f"<b>Yes, slightly.</b> As a group they beat “always Buy” on every horizon (by up to {best:.0%} points)."
        else:
            verdict_line = ("<b>Mixed.</b> As a group they beat “always Buy” on " +
                            ", ".join(h.lower() for h, v in edges.items() if v > 0) + " and lose on " +
                            ", ".join(h.lower() for h, v in edges.items() if v <= 0) + ".")
        B.append(f"<div style='font-size:13px;color:#211c16;margin-bottom:8px;line-height:1.5'>{verdict_line}</div>")
    B.append("<table role=presentation width=100% cellpadding=0 cellspacing=0 style='font-size:12.5px;background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:6px 12px'>"
             "<tr style='color:#8a8073;font-size:11px'><td>horizon</td><td align=right>all accounts, averaged</td>"
             "<td align=right>“always Buy” rule</td><td align=right>difference</td><td align=right>calls scored</td></tr>")
    for h in HORIZONS:
        r = hr.get(h)
        if not r:
            continue
        edge = r["rate"] - r["baseline"]
        col = "#176c33" if edge > 0 else "#b3261e"
        B.append(f"<tr><td style='padding:3px 0'>{h.capitalize()}</td><td align=right>{r['rate']:.0%}</td><td align=right>{r['baseline']:.0%}</td>"
                 f"<td align=right style='color:{col};font-weight:600'>{edge:+.0%}</td><td align=right style='color:#8a8073'>{r['n']:,}</td></tr>")
    B.append("</table>")
    B.append("<div style='color:#8a8073;font-size:11px;margin-top:4px;line-height:1.45'>"
             "<b>all accounts, averaged</b> = every matured call from every account, scored right 1 · half-right ½ · wrong 0, then averaged. "
             "<b>“always Buy” rule</b> = the same calls, pretending each had said Buy (market went up 1 · flat ½ · down 0). "
             "Only the difference measures skill.</div>")

    def _trust_list(items):
        return "".join(f"<tr><td style='padding:2px 0'>@{_esc(t['handle'])}</td><td align=right style='font-weight:600'>{t['score']:.2f}</td>"
                       f"<td align=right style='color:#8a8073'>{t['n']} calls</td></tr>" for t in items)
    if d["top_trust"]:
        B.append("<table role=presentation width=100% cellpadding=0 cellspacing=0 style='font-size:12.5px;margin-top:8px'><tr valign=top>"
                 "<td width=50% style='padding-right:6px'><div style='color:#8a8073;font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:3px'>Most reliable</div>"
                 "<table role=presentation width=100% cellpadding=0 cellspacing=0 style='background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:4px 10px'>" + _trust_list(d["top_trust"]) + "</table></td>"
                 "<td width=50% style='padding-left:6px'><div style='color:#8a8073;font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:3px'>Least reliable</div>"
                 "<table role=presentation width=100% cellpadding=0 cellspacing=0 style='background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:4px 10px'>" + _trust_list(d.get("bottom_trust", [])) + "</table></td></tr></table>")
        B.append("<div style='color:#8a8073;font-size:11px;margin-top:4px'>trust = (points + 5) / (calls + 10), all cells pooled; 0.50 = no better than a coin, accounts with ≥ 10 matured calls only</div>")
    B.append(f"<div style='color:#8a8073;font-size:11px;margin-top:4px'>{fn['scored']} accounts scored · {fn['calls']:,} calls · {fn['outcomes']:,} matured · {fn['tweets']:,} tweets from {fn['active']}/{fn['accounts']} active accounts</div>")

    # audience: visitors (Vercel Web Analytics) → who (signed-in panel users) → what they look at (influencer clicks)
    B.append(_h2("👥 Audience"))
    if ext:
        d1, d7 = ext.get("d1", {}), ext.get("d7", {})
        B.append(_grid([
            _card("visitors 24 h", f"{d1.get('visitors') if d1.get('visitors') is not None else '—'}", f"{d1.get('pageviews') or 0} pageviews · finclator.com, anonymous"),
            _card("visitors 7 d", f"{d7.get('visitors') if d7.get('visitors') is not None else '—'}", f"{d7.get('pageviews') or 0} pageviews"),
        ]))
        if ext.get("countries"):
            B.append("<div style='font-size:12px;margin-top:6px;color:#5d554b'>7 d by country: " + ", ".join(f"{c} {n}" for c, n in ext["countries"]) + "</div>")
        if ext.get("paths"):
            B.append("<div style='font-size:12px;color:#5d554b'>7 d most visited pages: " + ", ".join(f"{_esc(p)} {n}" for p, n in ext["paths"]) + "</div>")
        B.append("<div style='color:#8a8073;font-size:11px;margin-top:2px'>Vercel Web Analytics counts anonymous visitors to the public site and the panel — it cannot say who they are.</div>")
    else:
        B.append("<div style='color:#8a8073;font-size:12.5px'>Web Analytics unavailable (VERCEL_API_TOKEN / VERCEL_PROJECT_ID / VERCEL_TEAM_ID unset or request failed).</div>")

    ck7, ck30 = d.get("clicks", {}).get("d7", {}), d.get("clicks", {}).get("d30", {})
    B.append("<div style='color:#8a8073;font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin:12px 0 3px'>Who is in the panel · 7 d</div>")
    if d.get("clickers"):
        B.append("<table role=presentation width=100% cellpadding=0 cellspacing=0 style='font-size:12.5px;background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:4px 10px'>"
                 + "".join(f"<tr><td style='padding:2px 0'>{_esc(c['who'])}</td><td align=right style='color:#8a8073'>{c['n']} clicks · {c['handles']} accounts</td></tr>" for c in d["clickers"])
                 + "</table>")
    else:
        B.append("<div style='font-size:12px;color:#5d554b'>No signed-in panel user clicked an influencer link in the last 7 days.</div>")

    B.append("<div style='color:#8a8073;font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin:12px 0 3px'>"
             f"Most clicked influencers · 7 d ({ck7.get('total', 0)} clicks) · 30 d ({ck30.get('total', 0)})</div>")
    if ck30.get("top"):
        seven = {t["handle"]: t["n"] for t in ck7.get("top", [])}
        B.append("<table role=presentation width=100% cellpadding=0 cellspacing=0 style='font-size:12.5px;background:#fff;border:1px solid #e8ded0;border-radius:10px;padding:4px 10px'>"
                 "<tr style='color:#8a8073;font-size:11px'><td>account</td><td align=right>7 d</td><td align=right>30 d</td><td align=right>users</td></tr>"
                 + "".join(f"<tr><td style='padding:2px 0'><a href='https://x.com/{_esc(t['handle'])}' style='color:#211c16'>@{_esc(t['handle'])}</a></td>"
                           f"<td align=right>{seven.get(t['handle'], 0)}</td><td align=right style='font-weight:600'>{t['n']}</td><td align=right style='color:#8a8073'>{t['who']}</td></tr>"
                           for t in ck30["top"]) + "</table>")
        B.append("<div style='color:#8a8073;font-size:11px;margin-top:4px'>clicks on any @account / tweet link, panel + public site · users = distinct signed-in panel users (site visitors are anonymous)</div>")
    else:
        B.append("<div style='font-size:12px;color:#5d554b'>No influencer clicks recorded yet — every @account / tweet link on the panel and the site reports here.</div>")

    # cost
    B.append(_h2("💸 Cost"))
    B.append("<div style='font-size:12.5px;color:#5d554b'>twitterapi.io balance: <b>" + (f"${credits:.2f}" if credits is not None else "n/a") +
             f"</b> · daily fetch ≈ $0.02–0.03 · classifier is local (Ollama, $0) · Jev gate {act['gated']['now']:,} calls/day</div>")
    if d["stale_accounts"]:
        B.append(f"<div style='font-size:11.5px;color:#8a8073;margin-top:4px'>active accounts silent 30 d ({len(d['stale_accounts'])}): " +
                 ", ".join("@" + _esc(h) for h in d["stale_accounts"][:12]) + ("…" if len(d["stale_accounts"]) > 12 else "") + "</div>")

    # watch list
    B.append(_h2("👀 Alarm thresholds"))
    B.append(f"<ul style='margin:0;padding-left:18px;font-size:12px;color:#5d554b;line-height:1.5'>"
             f"<li>matrix.json / deploy older than {MATRIX_STALE_H} h → daily job or deploy failed</li>"
             f"<li>newest tweet older than {FETCH_STALE_H} h → fetch broken</li>"
             f"<li>pending classification &gt; {PENDING_MAX} → Ollama / classify stage</li>"
             f"<li>any price older than {PRICE_STALE_D} d → Yahoo</li>"
             f"<li>one account ≥ {TOP_SHARE_MAX:.0%} of a cell · replies/RTs &gt; 0 · balance &lt; ${CREDITS_MIN_USD:.0f}</li></ul>")
    B.append("<div style='color:#8a8073;font-size:11px;margin-top:22px;border-top:1px solid #e8ded0;padding-top:10px'>"
             "Internal operations report · <a href='https://finclator.com/panel' style='color:#c77a06'>open panel</a> · "
             "<a href='https://finclator.com/panel/matrix' style='color:#c77a06'>matrix</a> · "
             "<a href='https://finclator.com/panel/audit' style='color:#c77a06'>audit</a></div>"
             "</td></tr></table></td></tr></table></body></html>")
    return subject, "".join(B)


# ── send ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def send_email(to: list[str], subject: str, html: str) -> dict:
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return {"skipped": "no RESEND_API_KEY"}
    frm = os.environ.get("MAIL_FROM", "Finclator <admin@finclator.com>")
    body = json.dumps({"from": frm, "to": to, "subject": subject, "html": html}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "finclator-report/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:400]}


def recipients() -> list[str]:
    raw = os.environ.get("OPS_REPORT_EMAIL") or os.environ.get("OWNER_EMAIL") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


def run(conn=None, dry: bool = False, now: datetime | None = None) -> dict:
    """Build, (send), persist labels for tomorrow's delta. Returns a JSON-able summary (+ html when dry).
    A dropped Neon connection (psycopg OperationalError) is retried once on a fresh connection."""
    now = now or datetime.now(timezone.utc)
    if conn is not None:
        return _run(conn, dry, now)
    for attempt in (1, 2):
        conn = connect()
        try:
            return _run(conn, dry, now)
        except Exception as e:  # noqa: BLE001
            if attempt == 2 or type(e).__name__ != "OperationalError":
                raise
            log(f"report: connection dropped ({e}); retrying on a fresh connection")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError("unreachable")


def _run(conn, dry: bool, now: datetime) -> dict:
    d = gather(conn, now)
    ext = vercel_analytics(now)
    credits = vendor_credits()
    P = problems(d, ext, credits, now)
    prev = load_state(conn)
    subject, html = render(d, ext, credits, P, prev, now)
    out = {"subject": subject, "problems": P, "recipients": recipients(), "sent": None}
    if dry:
        out["html"] = html
        return out
    out["sent"] = send_email(out["recipients"], subject, html) if out["recipients"] else {"skipped": "no recipients"}
    out["report_id"] = save_report(conn, now, subject, out["recipients"], out["sent"], P,
                                   {k: v["label"] for k, v in d["matrix"]["cells"].items()}, html)
    log(f"report: #{out['report_id']} {subject} → {out['recipients']} {out['sent']}")
    return out


if __name__ == "__main__":
    import sys
    r = run(dry="--send" not in sys.argv)
    if "html" in r:
        Path(ROOT / "data" / "report.html").write_text(r.pop("html"))
        print("wrote data/report.html")
    print(json.dumps(r, indent=1))
