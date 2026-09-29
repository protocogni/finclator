"""Tweet acquisition: roster sync, CSV backfill (TwExportly), and twitterapi.io incremental fetch.

Cost model (twitterapi.io): 15 credits per tweet RETURNED, 15-credit floor per call, 100,000 credits = $1. So the
design goal is never to receive a tweet we already store or will discard:

* one primitive — `advanced_search` with exact `since_time`/`until_time` epoch bounds — for every account, so a
  quiet account costs the 15-credit floor instead of a full timeline page (`last_tweets` has no since parameter);
* per-account watermark `accounts.last_fetch_at` (ISO UTC) with a small overlap for search-index lag, deduped by
  the tweet-id primary key;
* heavy posters (`rate_per_year` > SAMPLE_ABOVE_PER_YEAR, or `sampling` already set) get the asset keywords in the
  query (server-side prefilter): we pay only for tweets that can reach the classifier. Once a month one unfiltered
  timeline page keeps an eye on vocabulary the keyword clause misses;
* a credit floor aborts the run before it can drain the balance.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import yaml

from .db import connect, log
from .prefilter import is_relevant

ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "roster.yaml"
API = "https://api.twitterapi.io"

BACKFILL_YEARS = 3                # how far back a never-fetched account is pulled
SAMPLE_ABOVE_PER_YEAR = 1000      # originals/yr; above this the search carries the asset keywords (server-side prefilter)
WINDOW_DAYS = 30                  # long spans are split into windows so pagination stays bounded per call
OVERLAP = timedelta(hours=6)      # re-query this much before the watermark: search index lag; PK dedups
MAX_PAGES_PER_WINDOW = 100        # 20 tweets/page → ≤2,000 tweets ($0.30) per window, a hard cap on a runaway account
MIN_CREDITS = int(os.environ.get("FINCLATOR_MIN_CREDITS", "100000"))  # 100k = $1; abort the run below this
COVERAGE_DAY = 1                  # day of month: unfiltered timeline page for keyword accounts + followers refresh

# Server-side keyword filter for heavy posters. X search has no wildcards/stemming, so Turkish suffix forms are listed
# explicitly; mirrors the prefilter vocabulary (`_ASSET_PATTERNS`) as closely as the query syntax allows. A monthly
# unfiltered coverage page per keyword account catches what this misses.
_KW = ("bitcoin OR btc OR sats OR satoshi OR kripto OR koin OR coin OR "
       "gold OR xau OR xauusd OR gld OR comex OR bullion OR \"precious metals\" OR "
       "altın OR altin OR altının OR altına OR altında OR altındaki OR altınlar OR altını OR altınlar OR "
       "\"ons altın\" OR \"gram altın\" OR onsaltın OR gramaltın OR \"değerli metal\" OR \"kıymetli metal\" OR "
       "spx OR spy OR sp500 OR \"s&p\" OR \"s&p500\" OR es_f OR nasdaq OR ndx OR qqq OR dow OR djia OR russell OR "
       "\"wall street\" OR wallstreet OR \"us stocks\" OR \"us equities\" OR us500 OR nvidia OR nvda OR \"mag 7\" OR "
       "magnificent OR hisse OR hisseler OR hisseleri OR borsa OR borsalar OR borsaları OR borsada OR endeks OR endeksi")
SAMPLE_QUERY = f"({_KW})"
LAST_COST: dict[str, int] = {}     # per-handle credit estimate (15 × tweets received + requests) of the current process


# ---------- roster ----------

def sync_roster(conn: sqlite3.Connection) -> list[dict]:
    accounts = yaml.safe_load(ROSTER.read_text())["accounts"]
    for a in accounts:
        conn.execute(
            """INSERT INTO accounts(handle, display_name, school, language, active, updated_at)
               VALUES(?,?,?,?,1,?)
               ON CONFLICT(handle) DO UPDATE SET display_name=excluded.display_name,
                 school=excluded.school, language=excluded.language, active=1""",
            (a["handle"].lower(), a.get("display_name"), a["school"], a.get("language", "en"),
             datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    return accounts


# ---------- insert ----------

def _insert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        if r["is_reply"] or r["text"].startswith("RT @"):
            continue  # originals only — enforced here regardless of source
        rel, assets = is_relevant(r["text"], False)
        cur = conn.execute(
            """INSERT OR IGNORE INTO tweets(tweet_id, handle, created_at, text, is_reply, lang, source, assets_hint, relevant)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (r["id"], r["handle"], r["created_at"], r["text"], int(r["is_reply"]), r.get("lang"),
             r["source"], ",".join(assets), int(rel)),
        )
        n += cur.rowcount
    conn.commit()
    return n


# ---------- CSV backfill ----------

def import_csv(conn: sqlite3.Connection, path: Path, handle: str) -> int:
    handle = handle.lower()
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            tid = r["tweet_id"].strip("'\" ")
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            rows.append({
                "id": tid, "handle": handle, "created_at": ts.isoformat(), "text": r["text"],
                "is_reply": r.get("type", "") == "Reply", "lang": r.get("language"), "source": "csv",
            })
    return _insert(conn, rows)


# ---------- twitterapi.io ----------

def _client() -> httpx.Client:
    key = os.environ.get("TWITTERAPI_IO_KEY") or _dotenv("TWITTERAPI_IO_KEY")
    if not key:
        raise RuntimeError("TWITTERAPI_IO_KEY not set")
    return httpx.Client(base_url=API, headers={"X-API-Key": key}, timeout=30)


def _dotenv(name: str) -> str | None:
    p = ROOT / ".env"
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return None


def _get(c: httpx.Client, path: str, **params) -> dict:
    for attempt in range(5):
        r = c.get(path, params=params)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 402:
            raise RuntimeError("twitterapi.io: out of credits — top up at https://twitterapi.io")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited on {path}")


def credits(c: httpx.Client | None = None) -> int:
    """Remaining twitterapi.io credits (100,000 = $1)."""
    with (c or _client()) as cc:
        info = _get(cc, "/oapi/my/info")
    return int(info.get("recharge_credits", 0)) + int(info.get("total_bonus_credits", 0))


def _keep(t: dict) -> bool:
    """Original authored content only: no replies, no retweets."""
    return not t.get("isReply") and not t.get("retweeted_tweet") and not t.get("text", "").startswith("RT @")


def _norm(t: dict, handle: str) -> dict:
    ts = parsedate_to_datetime(t["createdAt"]).astimezone(timezone.utc)
    return {
        "id": str(t["id"]), "handle": handle, "created_at": ts.isoformat(), "text": t.get("text", ""),
        "is_reply": bool(t.get("isReply")), "lang": t.get("lang"), "source": "twitterapi",
    }


def _search_window(c: httpx.Client, handle: str, start: datetime, end: datetime, keywords: bool,
                   max_pages: int = MAX_PAGES_PER_WINDOW) -> tuple[list[dict], int]:
    """Original tweets of `handle` in [start, end) via advanced search (exact epoch bounds), optionally
    keyword-filtered. Returns (rows, requests_made)."""
    q = f"from:{handle} -filter:replies -filter:retweets since_time:{int(start.timestamp())} until_time:{int(end.timestamp())}"
    if keywords:
        q += f" {SAMPLE_QUERY}"
    out, cursor, calls = [], "", 0
    for _ in range(max_pages):
        time.sleep(1.0)
        r = _get(c, "/twitter/tweet/advanced_search", query=q, queryType="Latest", cursor=cursor)
        calls += 1
        raw = r.get("tweets") or []
        page = [t for t in raw if _keep(t)]
        out += [_norm(t, handle) for t in page]
        if not raw or not r.get("has_next_page") or not r.get("next_cursor"):  # `raw`, not `page`: a page of RTs is not the end
            break
        cursor = r["next_cursor"]
    return out, calls


def _is_keyword_account(row) -> bool:
    return bool(row["sampling"]) or (row["rate_per_year"] or 0) > SAMPLE_ABOVE_PER_YEAR


def walk_timeline(conn: sqlite3.Connection, handle: str, years: int = BACKFILL_YEARS, max_pages: int = 400) -> dict:
    """Fallback for accounts that `advanced_search from:<handle>` under-returns (X's search index is incomplete for
    low-engagement accounts): cursor-walk `/twitter/user/last_tweets` (replies excluded server-side, RTs dropped by
    `_keep`) back to the `years` cutoff. Same insert gate as the search path; sets the watermark so the daily search
    windows take over from here. Cost ≈ 15 × (tweets returned + pages)."""
    handle = handle.lower()
    cutoff = datetime.now(timezone.utc) - timedelta(days=365 * years)
    received = inserted = pages = 0
    cursor = ""
    with _client() as c:
        for _ in range(max_pages):
            time.sleep(1.0)
            r = _get(c, "/twitter/user/last_tweets", userName=handle, cursor=cursor, includeReplies="false")
            pages += 1
            raw = (r.get("data") or {}).get("tweets") or []
            rows = [_norm(t, handle) for t in raw if _keep(t)]
            received += len(rows)
            inserted += _insert(conn, rows)
            conn.commit()
            oldest = min((datetime.fromisoformat(x["created_at"]) for x in rows), default=None)
            if not raw or not r.get("has_next_page") or not r.get("next_cursor") or (oldest and oldest < cutoff):
                break
            cursor = r["next_cursor"]
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE accounts SET last_fetch_at=?, updated_at=? WHERE handle=?", (now.isoformat(), now.isoformat(), handle))
    conn.commit()
    cost = 15 * (received + pages)
    log(f"  {handle}: timeline walk → {received} received, {inserted} new, {pages} pages ≈ {cost} cr")
    return {"received": received, "inserted": inserted, "pages": pages, "credits": cost}


def _since(conn: sqlite3.Connection, handle: str, row, now: datetime) -> datetime:
    """Start of the span to fetch: watermark − overlap; seeded from the newest stored tweet; else BACKFILL_YEARS ago."""
    wm = row["last_fetch_at"]
    if not wm:
        wm = conn.execute("SELECT max(created_at) FROM tweets WHERE handle=?", (handle,)).fetchone()[0]
    if wm:
        return datetime.fromisoformat(wm).astimezone(timezone.utc) - OVERLAP
    return now - timedelta(days=365 * BACKFILL_YEARS)


def fetch_account(conn: sqlite3.Connection, handle: str, since: datetime | None = None,
                  coverage: bool = False) -> int:
    """Fetch every original tweet of `handle` from `since` (default: its watermark) to now, in WINDOW_DAYS windows.
    Keyword accounts get the asset vocabulary in the query. `coverage` adds one unfiltered timeline page (keyword
    accounts) and refreshes followers. Advances `last_fetch_at` only after the whole span succeeded."""
    handle = handle.lower()
    row = conn.execute("SELECT last_fetch_at, sampling, rate_per_year FROM accounts WHERE handle=?", (handle,)).fetchone()
    if row is None:
        raise ValueError(f"unknown account {handle}")
    now = datetime.now(timezone.utc)
    start = since or _since(conn, handle, row, now)
    kw = _is_keyword_account(row)
    inserted = received = calls = 0
    long_span = (now - start) > timedelta(days=WINDOW_DAYS)
    with _client() as c:
        w = start
        while w < now:
            w_end = min(w + timedelta(days=WINDOW_DAYS), now)
            rows, n_calls = _search_window(c, handle, w, w_end, keywords=kw)
            received += len(rows)
            calls += n_calls
            inserted += _insert(conn, rows)
            if long_span:
                log(f"  {handle}: window {w.date()}→{w_end.date()} {len(rows)} tweets, {n_calls} req "
                    f"(running: {received} received, {inserted} new)")
            w = w_end
        if coverage:
            time.sleep(1.0)
            info = _get(c, "/twitter/user/info", userName=handle).get("data") or {}
            if info.get("followers"):
                conn.execute("UPDATE accounts SET followers=? WHERE handle=?", (info["followers"], handle))
            if kw:
                time.sleep(1.0)
                resp = _get(c, "/twitter/user/last_tweets", userName=handle, cursor="", includeReplies="false")
                page = [_norm(t, handle) for t in ((resp.get("data") or {}).get("tweets") or []) if _keep(t)]
                extra = _insert(conn, page)
                received += len(page)
                calls += 1
                if extra:
                    log(f"  {handle}: coverage page found {extra} tweets the keyword query missed")
                inserted += extra
    conn.execute("UPDATE accounts SET last_fetch_at=?, sampling=?, updated_at=? WHERE handle=?",
                 (now.isoformat(), "keyword" if kw else None, now.isoformat(), handle))
    conn.commit()
    days = (now - start).total_seconds() / 86400
    cost = 15 * (received + calls)
    LAST_COST[handle] = cost
    log(f"  {handle}: {'kw' if kw else 'full'} {days:.1f}d → {received} received, {inserted} new, "
        f"{calls} req ≈ {cost} cr")
    return inserted


def fetch_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Incremental fetch for every active account; aborts before spending if the balance is under MIN_CREDITS."""
    before = credits()
    LAST_COST.clear()
    if before < MIN_CREDITS:
        raise RuntimeError(f"twitterapi.io balance {before:,} credits < floor {MIN_CREDITS:,} — top up before fetching")
    coverage = datetime.now(timezone.utc).day == COVERAGE_DAY
    out = {}
    for a in conn.execute("SELECT handle FROM accounts WHERE active=1 ORDER BY handle").fetchall():
        h = a["handle"]
        try:
            out[h] = fetch_account(conn, h, coverage=coverage)
        except Exception as e:  # noqa: BLE001 — one account must not stop the run; watermark stays, retried next run
            log(f"  {h}: ERROR {e}")
            out[h] = -1
    after = credits()
    est = sum(LAST_COST.values())
    log(f"fetch: {sum(v for v in out.values() if v > 0)} new tweets, ≈{est:,} credits (${est / 1e5:.3f}) by count; "
        f"balance ${after / 1e5:.2f} (vendor balance updates lazily)")
    return out


if __name__ == "__main__":
    import sys
    conn = connect()
    sync_roster(conn)
    if len(sys.argv) > 2 and sys.argv[1] == "csv":
        # python -m src.fetch csv data/file.csv handle
        print(import_csv(conn, Path(sys.argv[2]), sys.argv[3]), "inserted")
    elif len(sys.argv) > 1:
        print(fetch_account(conn, sys.argv[1], coverage="--coverage" in sys.argv), "inserted")
    else:
        print(fetch_all(conn))
