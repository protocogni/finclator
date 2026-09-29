"""Storage. Two backends behind one `connect()`:

- SQLite (`data/finclator.db`) when `DATABASE_URL` is unset — the original single-file, zero-ops mode.
- Postgres (Neon via Vercel) when `DATABASE_URL` is set — the hosted panel and the weekly run share one live DB.

Every module writes SQLite-flavoured SQL; `_PgConnection` rewrites the small set of idioms that differ
(`?` placeholders, `INSERT OR IGNORE/REPLACE`, `datetime('now')`, `instr()`, `random()`) so the query sites stay
untouched. Rows behave like `sqlite3.Row` on both backends (index by position or name, `.keys()`).
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "finclator.db"
LOG_PATH = DB_PATH.parent / "pipeline.log"


def log(msg: str) -> None:
    """Timestamped line to stdout and data/pipeline.log (the admin page tails it)."""
    from datetime import datetime, timezone
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # read-only filesystem (hosted panel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    handle        TEXT PRIMARY KEY,           -- lowercase, no @
    display_name  TEXT,
    school        TEXT NOT NULL,
    language      TEXT NOT NULL DEFAULT 'en',
    active        INTEGER NOT NULL DEFAULT 1,
    last_tweet_id TEXT,                       -- legacy since_id watermark (unused; see last_fetch_at)
    followers     INTEGER,
    rate_per_year INTEGER,                    -- measured originals/yr at backfill
    sampling      TEXT,                       -- NULL = full timeline; 'keyword' = asset keywords in the search query
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS tweets (
    id          TEXT PRIMARY KEY,             -- tweet id (string, exact)
    handle      TEXT NOT NULL REFERENCES accounts(handle),
    created_at  TEXT NOT NULL,                -- ISO-8601 UTC
    text        TEXT NOT NULL,
    is_reply    INTEGER NOT NULL DEFAULT 0,
    lang        TEXT,
    source      TEXT NOT NULL,                -- 'csv' | 'twitterapi'
    assets_hint TEXT NOT NULL DEFAULT '',     -- prefilter: 'BTC,GOLD'
    relevant    INTEGER NOT NULL DEFAULT 0,   -- prefilter passed → eligible for LLM
    classified  INTEGER NOT NULL DEFAULT 0    -- legacy flag; per-model state lives in classified_by
);
CREATE TABLE IF NOT EXISTS classified_by (
    tweet_id TEXT NOT NULL REFERENCES tweets(id),
    model    TEXT NOT NULL,
    at       TEXT,
    PRIMARY KEY (tweet_id, model)
);
CREATE INDEX IF NOT EXISTS ix_tweets_handle_created ON tweets(handle, created_at);
CREATE INDEX IF NOT EXISTS ix_tweets_pending ON tweets(relevant, classified);

-- Stage 2a: Jev decision-model gate, one row per (tweet, Jev version). p_call = calibrated is_call probability,
-- stances = JSON {asset: none|up|down|neutral}. The hybrid classifier sends only passing tweets to the text model.
CREATE TABLE IF NOT EXISTS gate (
    tweet_id TEXT NOT NULL REFERENCES tweets(id),
    model    TEXT NOT NULL,
    p_call   REAL NOT NULL,
    stances  TEXT,
    at       TEXT,
    PRIMARY KEY (tweet_id, model)
);

-- One row per (tweet, asset) explicit directional call. Non-calls are not stored here.
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    tweet_id    TEXT NOT NULL REFERENCES tweets(id),
    handle      TEXT NOT NULL,
    asset       TEXT NOT NULL,                -- BTC | GOLD | SPX
    direction   TEXT NOT NULL,                -- BUY | NEUTRAL | SELL
    horizon     TEXT NOT NULL,                -- SHORT | MEDIUM | LONG
    confidence  REAL NOT NULL,
    price_target REAL,                        -- explicit level if stated (same units as prices table)
    quote       TEXT,                         -- exact span justifying the label (audit trail)
    called_at   TEXT NOT NULL,                -- = tweet created_at
    model       TEXT NOT NULL,               -- classifier that produced this call; every downstream table is per-model
    UNIQUE(tweet_id, asset, model)
);
CREATE INDEX IF NOT EXISTS ix_calls_cell ON calls(model, asset, horizon, called_at);

CREATE TABLE IF NOT EXISTS prices (
    asset  TEXT NOT NULL,
    date   TEXT NOT NULL,                     -- YYYY-MM-DD
    close  REAL NOT NULL,
    PRIMARY KEY (asset, date)
);

-- Filled by evaluate.py once a call's horizon has matured.
CREATE TABLE IF NOT EXISTS outcomes (
    call_id      INTEGER PRIMARY KEY REFERENCES calls(id),
    entry_date   TEXT NOT NULL,
    exit_date    TEXT NOT NULL,
    entry_close  REAL NOT NULL,
    exit_close   REAL NOT NULL,
    return_pct   REAL NOT NULL,
    threshold_pct REAL NOT NULL,              -- vol-scaled band used for NEUTRAL
    actual       TEXT NOT NULL,               -- BUY | NEUTRAL | SELL (what the market did)
    result       TEXT NOT NULL,               -- CORRECT | WRONG | PARTIAL  (direction)
    target_hit   INTEGER,                     -- 1 if price_target was touched within the horizon, 0 if not, NULL n/a
    extreme      REAL,                        -- max close (BUY) / min close (SELL) within horizon
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trust (
    model       TEXT NOT NULL,
    handle      TEXT NOT NULL,
    asset       TEXT NOT NULL,                -- or '*' for overall
    horizon     TEXT NOT NULL,                -- or '*' for overall
    n           INTEGER NOT NULL,
    correct     REAL NOT NULL,                -- CORRECT=1, PARTIAL=0.5
    score       REAL NOT NULL,                -- shrunk toward 0.5
    computed_at TEXT NOT NULL,
    PRIMARY KEY (model, handle, asset, horizon)
);

-- Influencer link clicks on the panel and the public site (which accounts people look at). Written by
-- admin.record_click via /api/click; `who` = signed-in panel email or NULL for an anonymous visitor.
CREATE TABLE IF NOT EXISTS clicks (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,                -- YYYY-MM-DD HH:MM:SS UTC
    handle      TEXT NOT NULL,
    src         TEXT,                         -- tab / page that carried the link: accounts, matrix, audit, site…
    page        TEXT,                         -- path the click happened on
    who         TEXT                          -- panel email or NULL
);
CREATE INDEX IF NOT EXISTS ix_clicks_at ON clicks(at);
"""

# Tables in FK order (migration + schema listing) and their conflict keys (for INSERT OR REPLACE → ON CONFLICT).
TABLES = ["accounts", "tweets", "classified_by", "gate", "calls", "prices", "outcomes", "trust", "clicks"]
CONFLICT_KEYS = {
    "accounts": ("handle",), "tweets": ("id",), "classified_by": ("tweet_id", "model"), "gate": ("tweet_id", "model"),
    "calls": ("tweet_id", "asset", "model"), "prices": ("asset", "date"), "outcomes": ("call_id",),
    "trust": ("model", "handle", "asset", "horizon"),
}
MIGRATIONS = (("accounts", "rate_per_year", "INTEGER"), ("accounts", "sampling", "TEXT"), ("accounts", "tier", "TEXT"),
              ("accounts", "last_fetch_at", "TEXT"),
              ("calls", "price_target", "REAL"), ("calls", "gate_p", "REAL"),
              ("outcomes", "target_hit", "INTEGER"), ("outcomes", "extreme", "REAL"))


def database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    p = DB_PATH.parent.parent / ".env"
    if p.exists():
        for ln in p.read_text().splitlines():
            if ln.startswith("DATABASE_URL="):
                return ln.split("=", 1)[1].strip().strip('"')
    return None


# ── Postgres backend ─────────────────────────────────────────────────────────────────────────────────────────────
class Row(tuple):
    """sqlite3.Row look-alike: r[0], r["col"], r.keys()."""

    def __new__(cls, cols, values):
        self = super().__new__(cls, values)
        self._cols = cols
        return self

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._cols[k])
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._cols)


def _row_factory(cursor):
    from decimal import Decimal
    cols = {d.name: i for i, d in enumerate(cursor.description or ())}

    def make(values):
        return Row(cols, tuple(float(v) if isinstance(v, Decimal) else v for v in values))
    return make


_RE_IGNORE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)", re.I)
_RE_REPLACE = re.compile(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I)
_RE_NOW_DELTA = re.compile(r"datetime\('now'\s*,\s*'([-+]?\d+)\s+(\w+)'\)", re.I)
_RE_INSTR = re.compile(r"\binstr\(([^,]+),\s*([^)]+)\)", re.I)
_PG_NOW = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"


def to_pg(sql: str) -> str:
    """Rewrite SQLite idioms used in this codebase into Postgres."""
    m = _RE_IGNORE.search(sql)
    if m:
        sql = _RE_IGNORE.sub(r"INSERT INTO \1", sql).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    m = _RE_REPLACE.search(sql)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        keys = CONFLICT_KEYS[table]
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in keys)
        sql = _RE_REPLACE.sub(rf"INSERT INTO {table}(\2)", sql).rstrip().rstrip(";")
        sql += f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {sets}"
    sql = _RE_NOW_DELTA.sub(lambda m: f"to_char((now() at time zone 'utc') + interval '{m.group(1)} {m.group(2)}', "
                                      "'YYYY-MM-DD HH24:MI:SS')", sql)
    sql = sql.replace("datetime('now')", _PG_NOW)
    sql = _RE_INSTR.sub(r"position(\2 in \1)", sql)
    sql = sql.replace("%", "%%").replace("?", "%s")
    return sql


class _PgCursorProxy:
    def __init__(self, cur):
        self._cur = cur

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PgConnection:
    """Minimal sqlite3.Connection-compatible façade over psycopg 3."""
    backend = "postgres"

    def __init__(self, url: str):
        import psycopg
        # connect_timeout + TCP keepalives: a Neon pooler that silently drops the socket otherwise leaves the client
        # blocked in recv for the kernel's default (~15 min) before psycopg raises OperationalError.
        self._c = psycopg.connect(url, row_factory=_row_factory, autocommit=False, connect_timeout=20,
                                  keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)

    # sqlite3 API surface used by the codebase
    def execute(self, sql: str, params=()):
        cur = self._c.cursor()
        cur.execute(to_pg(sql), tuple(params) if not isinstance(params, dict) else params)
        return _PgCursorProxy(cur)

    def executemany(self, sql: str, seq):
        cur = self._c.cursor()
        cur.executemany(to_pg(sql), [tuple(p) for p in seq])
        return _PgCursorProxy(cur)

    def executescript(self, script: str):
        clean = "\n".join(ln.split("--", 1)[0] for ln in script.splitlines())
        for stmt in [s.strip() for s in clean.split(";") if s.strip()]:
            self._c.execute(stmt)
        self._c.commit()

    def cursor(self):
        return self._c.cursor()

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()

    @property
    def row_factory(self):
        return _row_factory

    @row_factory.setter
    def row_factory(self, _):
        pass

    @property
    def raw(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        (self.rollback if exc[0] else self.commit)()


def pg_schema() -> str:
    """SCHEMA translated for Postgres: identity ids, float8 instead of float4."""
    s = SCHEMA.replace("id          INTEGER PRIMARY KEY,", "id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,")
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    return s


def columns(conn, table: str) -> list[str]:
    if getattr(conn, "backend", "sqlite") == "postgres":
        return [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? "
            "ORDER BY ordinal_position", (table,))]
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def primary_key(conn, table: str) -> set[str]:
    if getattr(conn, "backend", "sqlite") == "postgres":
        return set(CONFLICT_KEYS.get(table, ()))
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})") if r["pk"]}


def tables(conn) -> list[str]:
    if getattr(conn, "backend", "sqlite") == "postgres":
        return [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _migrate(conn) -> None:
    for table, col, typ in MIGRATIONS:
        if col not in columns(conn, table):
            if getattr(conn, "backend", "sqlite") == "postgres" and typ == "REAL":
                typ = "DOUBLE PRECISION"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


def connect(path: Path = DB_PATH, url: str | None = None):
    """Postgres when DATABASE_URL (env or .env) is set, else SQLite at `path`."""
    url = url if url is not None else database_url()
    if url:
        conn = _PgConnection(url)
        conn.executescript(pg_schema())
        _migrate(conn)
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def connect_sqlite(path: Path = DB_PATH) -> sqlite3.Connection:
    """Always the local SQLite file (migration source, offline tools)."""
    return connect(path, url="")
