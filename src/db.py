"""Storage. Postgres (Neon) is the source of truth — `DATABASE_URL` in the environment or `.env`.

Every module writes SQLite-flavoured SQL; `_PgConnection` rewrites the small set of idioms that differ
(`?` placeholders, `INSERT OR IGNORE/REPLACE`, `datetime('now')`, `instr()`, `random()`) so the query sites stay
untouched. Rows behave like `sqlite3.Row` on both backends (index by position or name, `.keys()`).
SQLite (`connect(url="")`) builds the same schema in a file or in memory — tests and offline tools only.

Schema rules: every table has `id` (BIGINT identity on Postgres, rowid alias on SQLite) as its primary key; the
natural key is a UNIQUE constraint (`INSERT OR IGNORE/REPLACE` conflicts resolve on it; see CONFLICT_KEYS); enums
are CHECKed; every reference is a foreign key. Additive columns go in `MIGRATIONS` (applied on every connect),
structural changes in `STEPS` (applied once on Postgres, recorded in `schema_migrations`).
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "finclator.db"          # SQLite path for tests / offline tools only
LOG_PATH = ROOT / "data" / "pipeline.log"


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


ASSETS = ("BTC", "GOLD", "SPX")
DIRECTIONS = ("BUY", "NEUTRAL", "SELL")
HORIZONS = ("SHORT", "MEDIUM", "LONG")
RESULTS = ("CORRECT", "PARTIAL", "WRONG")


def _in(vals) -> str:
    return "(" + ", ".join(f"'{v}'" for v in vals) + ")"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    handle        TEXT NOT NULL UNIQUE,      -- lowercase, no @
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
    id          INTEGER PRIMARY KEY,
    tweet_id    TEXT NOT NULL UNIQUE,         -- X tweet id (string, exact); queries alias it `AS id` for consumers
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
CREATE INDEX IF NOT EXISTS ix_tweets_handle_created ON tweets(handle, created_at);
CREATE INDEX IF NOT EXISTS ix_tweets_pending ON tweets(relevant, classified);
CREATE INDEX IF NOT EXISTS ix_tweets_created ON tweets(created_at);

CREATE TABLE IF NOT EXISTS classified_by (
    id       INTEGER PRIMARY KEY,
    tweet_id TEXT NOT NULL REFERENCES tweets(tweet_id),
    model    TEXT NOT NULL,
    at       TEXT,
    UNIQUE(tweet_id, model)
);
CREATE INDEX IF NOT EXISTS ix_classified_by_model ON classified_by(model, tweet_id);

-- Stage 2a: Jev decision-model gate, one row per (tweet, Jev version). p_call = calibrated is_call probability,
-- stances = JSON {{asset: none|up|down|neutral}}. The hybrid classifier sends only passing tweets to the text model.
CREATE TABLE IF NOT EXISTS gate (
    id       INTEGER PRIMARY KEY,
    tweet_id TEXT NOT NULL REFERENCES tweets(tweet_id),
    model    TEXT NOT NULL,
    p_call   REAL NOT NULL,
    stances  TEXT,
    at       TEXT,
    UNIQUE(tweet_id, model)
);

-- One row per (tweet, asset, model) explicit directional call. Non-calls are not stored here.
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    tweet_id    TEXT NOT NULL REFERENCES tweets(tweet_id),
    handle      TEXT NOT NULL REFERENCES accounts(handle),
    asset       TEXT NOT NULL CHECK (asset IN {_in(ASSETS)}),
    direction   TEXT NOT NULL CHECK (direction IN {_in(DIRECTIONS)}),
    horizon     TEXT NOT NULL CHECK (horizon IN {_in(HORIZONS)}),
    confidence  REAL NOT NULL,
    price_target REAL,                        -- explicit level if stated (same units as prices table)
    quote       TEXT,                         -- exact span justifying the label (audit trail)
    called_at   TEXT NOT NULL,                -- = tweet created_at
    model       TEXT NOT NULL,               -- classifier that produced this call; every downstream table is per-model
    gate_p      REAL,                         -- Jev p_call when the gate ran (NULL before the gate existed)
    UNIQUE(tweet_id, asset, model)
);
CREATE INDEX IF NOT EXISTS ix_calls_cell ON calls(model, asset, horizon, called_at);
CREATE INDEX IF NOT EXISTS ix_calls_handle ON calls(handle, model);

CREATE TABLE IF NOT EXISTS prices (
    id     INTEGER PRIMARY KEY,
    asset  TEXT NOT NULL CHECK (asset IN {_in(ASSETS)}),
    date   TEXT NOT NULL,                     -- YYYY-MM-DD
    close  REAL NOT NULL,
    UNIQUE(asset, date)
);

-- Filled by evaluate.py once a call's horizon has matured.
CREATE TABLE IF NOT EXISTS outcomes (
    id           INTEGER PRIMARY KEY,
    call_id      INTEGER NOT NULL UNIQUE REFERENCES calls(id),
    entry_date   TEXT NOT NULL,
    exit_date    TEXT NOT NULL,
    entry_close  REAL NOT NULL,
    exit_close   REAL NOT NULL,
    return_pct   REAL NOT NULL,
    threshold_pct REAL NOT NULL,              -- vol-scaled band used for NEUTRAL
    actual       TEXT NOT NULL CHECK (actual IN {_in(DIRECTIONS)}),   -- what the market did
    result       TEXT NOT NULL CHECK (result IN {_in(RESULTS)}),      -- direction outcome
    target_hit   INTEGER,                     -- 1 if price_target was touched within the horizon, 0 if not, NULL n/a
    extreme      REAL,                        -- max close (BUY) / min close (SELL) within horizon
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outcomes_exit ON outcomes(exit_date);

CREATE TABLE IF NOT EXISTS trust (
    id          INTEGER PRIMARY KEY,
    model       TEXT NOT NULL,
    handle      TEXT NOT NULL REFERENCES accounts(handle),
    asset       TEXT NOT NULL CHECK (asset IN {_in(ASSETS + ('*',))}),      -- '*' = overall
    horizon     TEXT NOT NULL CHECK (horizon IN {_in(HORIZONS + ('*',))}),  -- '*' = overall
    n           INTEGER NOT NULL,
    correct     REAL NOT NULL,                -- CORRECT=1, PARTIAL=0.5
    score       REAL NOT NULL,                -- shrunk toward 0.5
    computed_at TEXT NOT NULL,
    UNIQUE(model, handle, asset, horizon)
);

-- Every click on finclator.com and the panel (JS beacon → /api/click → admin.record_click). `kind` = influencer
-- (an x.com link to a roster account, `handle` set) | link | button; `who` = signed-in panel email or NULL;
-- `visitor` = random per-browser id (localStorage) so anonymous visitors can be counted without cookies.
CREATE TABLE IF NOT EXISTS clicks (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,                -- YYYY-MM-DD HH:MM:SS UTC
    kind        TEXT,                         -- influencer | link | button
    handle      TEXT REFERENCES accounts(handle),
    href        TEXT,                         -- target of the link (clipped)
    label       TEXT,                         -- visible text of the element (clipped)
    src         TEXT,                         -- tab / page that carried the link: accounts, matrix, audit, site…
    page        TEXT,                         -- path the click happened on
    who         TEXT,                         -- panel email or NULL
    visitor     TEXT                          -- anonymous per-browser id or NULL
);
CREATE INDEX IF NOT EXISTS ix_clicks_at ON clicks(at);
CREATE INDEX IF NOT EXISTS ix_clicks_handle ON clicks(handle, at);

-- One row per `src.run` (the daily pipeline): status + per-stage results as JSON.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                -- running | ok | failed
    stages      TEXT NOT NULL DEFAULT '{{}}', -- JSON {{stage: {{at, result}}}} in execution order
    error       TEXT,
    host        TEXT,
    args        TEXT                          -- JSON: flags the run was started with
);

-- One row per daily ops report (src/report.py); the newest row's labels feed tomorrow's "was BUY" delta.
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    subject     TEXT NOT NULL,
    recipients  TEXT,                         -- JSON list
    sent        TEXT,                         -- JSON: mail API response
    problems    TEXT,                         -- JSON list
    labels      TEXT NOT NULL,                -- JSON {{cell: label}}
    html        TEXT                          -- the rendered email
);
"""

# Tables in FK order and their natural (conflict) keys — INSERT OR REPLACE → ON CONFLICT (…) DO UPDATE.
TABLES = ["accounts", "tweets", "classified_by", "gate", "calls", "prices", "outcomes", "trust", "clicks", "runs",
          "reports"]
CONFLICT_KEYS = {
    "accounts": ("handle",), "tweets": ("tweet_id",), "classified_by": ("tweet_id", "model"),
    "gate": ("tweet_id", "model"), "calls": ("tweet_id", "asset", "model"), "prices": ("asset", "date"),
    "outcomes": ("call_id",), "trust": ("model", "handle", "asset", "horizon"), "schema_migrations": ("name",),
}
# Additive columns (applied on every connect, both backends).
MIGRATIONS = (("accounts", "rate_per_year", "INTEGER"), ("accounts", "sampling", "TEXT"), ("accounts", "tier", "TEXT"),
              ("accounts", "last_fetch_at", "TEXT"),
              ("calls", "price_target", "REAL"), ("calls", "gate_p", "REAL"),
              ("outcomes", "target_hit", "INTEGER"), ("outcomes", "extreme", "REAL"),
              ("clicks", "kind", "TEXT"), ("clicks", "href", "TEXT"), ("clicks", "label", "TEXT"),
              ("clicks", "visitor", "TEXT"))


def database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    p = ROOT / ".env"
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


_RE_ID_PK = re.compile(r"\bid\s+INTEGER PRIMARY KEY\b")
_PG_ID = "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"


def pg_schema() -> str:
    """SCHEMA translated for Postgres: identity ids, float8 instead of float4."""
    s = _RE_ID_PK.sub(_PG_ID, SCHEMA)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    return s


def _is_pg(conn) -> bool:
    return getattr(conn, "backend", "sqlite") == "postgres"


def columns(conn, table: str) -> list[str]:
    if _is_pg(conn):
        return [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? "
            "ORDER BY ordinal_position", (table,))]
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def primary_key(conn, table: str) -> set[str]:
    """Primary-key columns, read from the catalog (not from code)."""
    if _is_pg(conn):
        return {r[0] for r in conn.execute(
            """SELECT k.column_name FROM information_schema.table_constraints c
               JOIN information_schema.key_column_usage k ON k.constraint_name=c.constraint_name AND k.table_name=c.table_name
               WHERE c.table_schema='public' AND c.table_name=? AND c.constraint_type='PRIMARY KEY'""", (table,))}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})") if r["pk"]}


def unique_keys(conn, table: str) -> list[tuple[str, ...]]:
    """Natural keys: every UNIQUE constraint on the table (the primary key excluded)."""
    if _is_pg(conn):
        rows = conn.execute(
            """SELECT c.constraint_name, k.column_name FROM information_schema.table_constraints c
               JOIN information_schema.key_column_usage k ON k.constraint_name=c.constraint_name AND k.table_name=c.table_name
               WHERE c.table_schema='public' AND c.table_name=? AND c.constraint_type='UNIQUE'
               ORDER BY c.constraint_name, k.ordinal_position""", (table,)).fetchall()
        by_name: dict[str, list[str]] = {}
        for r in rows:
            by_name.setdefault(r[0], []).append(r[1])
        return [tuple(v) for v in by_name.values()]
    out: list[tuple[str, ...]] = []
    for ix in conn.execute(f"PRAGMA index_list({table})"):
        if ix["unique"] and ix["origin"] == "u":
            out.append(tuple(r["name"] for r in conn.execute(f"PRAGMA index_info({ix['name']})")))
    return out


def tables(conn) -> list[str]:
    if _is_pg(conn):
        return [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' "
            "ORDER BY 1")]
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _migrate(conn) -> None:
    for table, col, typ in MIGRATIONS:
        if col not in columns(conn, table):
            if _is_pg(conn) and typ == "REAL":
                typ = "DOUBLE PRECISION"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


# ── structural migrations (Postgres only; each step is idempotent and recorded in schema_migrations) ─────────────
def _pg_constraints(conn, table: str, ctype: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid=?::regclass AND contype=?", (table, ctype))]


def _step_surrogate_ids(conn) -> None:
    """Every table: `id` identity PK, the old PK becomes a UNIQUE constraint, FKs re-pointed. tweets.id → tweet_id."""
    have = set(tables(conn))
    if "tweets" in have and "tweet_id" not in columns(conn, "tweets"):
        conn.execute("ALTER TABLE tweets RENAME COLUMN id TO tweet_id")
    for t in ("tweets", "classified_by", "gate", "calls", "outcomes", "trust", "clicks"):   # FKs recreated below
        if t in have:
            for c in _pg_constraints(conn, t, "f"):
                conn.execute(f"ALTER TABLE {t} DROP CONSTRAINT {c}")
    natural = {"accounts": "handle", "tweets": "tweet_id", "classified_by": "tweet_id, model", "gate": "tweet_id, model",
               "prices": "asset, date", "outcomes": "call_id", "trust": "model, handle, asset, horizon"}
    for t, key in natural.items():
        if t not in have or "id" in columns(conn, t):
            continue
        for c in _pg_constraints(conn, t, "p"):
            conn.execute(f"ALTER TABLE {t} DROP CONSTRAINT {c}")
        conn.execute(f"ALTER TABLE {t} ADD CONSTRAINT {t}_{key.replace(', ', '_')}_key UNIQUE ({key})")
        conn.execute(f"ALTER TABLE {t} ADD COLUMN {_PG_ID}")
    fks = [("tweets", "handle", "accounts(handle)"), ("classified_by", "tweet_id", "tweets(tweet_id)"),
           ("gate", "tweet_id", "tweets(tweet_id)"), ("calls", "tweet_id", "tweets(tweet_id)"),
           ("calls", "handle", "accounts(handle)"), ("outcomes", "call_id", "calls(id)"),
           ("trust", "handle", "accounts(handle)")]
    for t, col, ref in fks:
        if t in have:
            conn.execute(f"ALTER TABLE {t} ADD CONSTRAINT {t}_{col}_fkey FOREIGN KEY ({col}) REFERENCES {ref}")


def _step_checks(conn) -> None:
    have = set(tables(conn))
    checks = [("calls", "asset", ASSETS), ("calls", "direction", DIRECTIONS), ("calls", "horizon", HORIZONS),
              ("prices", "asset", ASSETS), ("outcomes", "actual", DIRECTIONS), ("outcomes", "result", RESULTS),
              ("trust", "asset", ASSETS + ("*",)), ("trust", "horizon", HORIZONS + ("*",))]
    for t, col, vals in checks:
        name = f"{t}_{col}_check"
        if t in have and name not in _pg_constraints(conn, t, "c"):
            conn.execute(f"ALTER TABLE {t} ADD CONSTRAINT {name} CHECK ({col} IN {_in(vals)})")


def _step_clicks_v2(conn) -> None:
    """clicks: handle nullable (non-influencer clicks) + FK + new columns; report_state → reports."""
    have = set(tables(conn))
    if "clicks" in have:
        for col, typ in (("kind", "TEXT"), ("href", "TEXT"), ("label", "TEXT"), ("visitor", "TEXT")):
            if col not in columns(conn, "clicks"):
                conn.execute(f"ALTER TABLE clicks ADD COLUMN {col} {typ}")
        conn.execute("ALTER TABLE clicks ALTER COLUMN handle DROP NOT NULL")
        if "clicks_handle_fkey" not in _pg_constraints(conn, "clicks", "f"):
            conn.execute("ALTER TABLE clicks ADD CONSTRAINT clicks_handle_fkey FOREIGN KEY (handle) REFERENCES accounts(handle)")
        conn.execute("UPDATE clicks SET kind='influencer' WHERE kind IS NULL AND handle IS NOT NULL")
    if "report_state" in have:
        import json
        conn.executescript(_RE_ID_PK.sub(_PG_ID, _table_ddl("reports")))
        for r in conn.execute("SELECT value, updated_at FROM report_state WHERE key='daily_report'").fetchall():
            try:
                st = json.loads(r[0])
            except (ValueError, TypeError):
                st = {}
            conn.execute("INSERT INTO reports(at, subject, labels) VALUES (?, ?, ?)",
                         ((st.get("at") or r[1] or "")[:19].replace("T", " "), "(migrated from report_state)",
                          json.dumps(st.get("labels", {}))))
        conn.execute("DROP TABLE report_state")


STEPS = (("001_surrogate_ids", _step_surrogate_ids), ("002_checks", _step_checks), ("003_clicks_v2", _step_clicks_v2))


def _table_ddl(table: str) -> str:
    """The CREATE TABLE statement for one table, from SCHEMA."""
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", SCHEMA, re.S)
    if not m:
        raise KeyError(table)
    return m.group(0)


def _apply_steps(conn) -> list[str]:
    """Run every STEPS entry not yet in schema_migrations, one transaction each. Returns the names applied."""
    conn.executescript(_RE_ID_PK.sub(_PG_ID, _table_ddl("schema_migrations")))
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for name, fn in STEPS:
        if name in done:
            continue
        try:
            fn(conn)
            conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?, datetime('now'))", (name,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(name)
        log(f"db: applied {name}")
    return applied


def connect(path: Path = DB_PATH, url: str | None = None):
    """Postgres when DATABASE_URL (env or .env) is set, else SQLite at `path` (tests / offline tools)."""
    url = url if url is not None else database_url()
    if url:
        conn = _PgConnection(url)
        _apply_steps(conn)
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
    """Always a local SQLite file (tests, offline tools) — never the production data."""
    return connect(path, url="")
