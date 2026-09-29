"""Schema invariants after the surrogate-id change: every table has `id` as PK, tweets are keyed by `tweet_id`, and the
pending/audit queries resolve `tweet_id` (a bare `id` would silently compare the surrogate against X ids)."""
from src import audit, classify, db, gate


def test_every_table_has_id_pk(conn):
    for t in db.tables(conn):
        assert db.primary_key(conn, t) == {"id"}, t


def test_natural_keys(conn):
    assert db.unique_keys(conn, "tweets") == [("tweet_id",)]
    assert ("tweet_id", "asset", "model") in db.unique_keys(conn, "calls")
    assert ("tweet_id", "model") in db.unique_keys(conn, "classified_by")


def test_pending_uses_tweet_id(conn):
    # fixture: tweets 1,2,3 relevant; 1,2,3 classified by 'm' → nothing pending for m; all three for another model
    assert [r["id"] for r in classify.pending(conn, model="m")] == []
    assert sorted(r["id"] for r in classify.pending(conn, model="other")) == ["1", "2", "3"]
    assert gate.lookup(conn, ["1", "2"]).keys() == {"1"}


def test_audit_renders_non_calls_sample(conn):
    html = audit.body(conn, model="m")
    assert "x.com/alice/status/2" in html          # tweet 2: classified, no call → in the non-calls sample


def test_insert_dedupes_on_natural_key(conn):
    conn.execute("INSERT OR IGNORE INTO tweets(tweet_id, handle, created_at, text, source) VALUES('1','alice','2024','dup','csv')")
    assert conn.execute("SELECT count(*) FROM tweets").fetchone()[0] == 4
    conn.execute("INSERT OR IGNORE INTO prices(asset, date, close) VALUES('BTC','2026-09-22',1)")
    assert conn.execute("SELECT close FROM prices WHERE asset='BTC'").fetchone()[0] == 85000


def test_check_constraints(conn):
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO calls(tweet_id, handle, asset, direction, horizon, confidence, called_at, model) "
                     "VALUES('1','alice','BTC','BUY','NEUTRAL',0.5,'2024','m')")
