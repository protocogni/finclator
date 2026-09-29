"""score.hit_rates: hit rate per horizon vs an always-BUY baseline on the same matured outcomes."""
from src import db, score


def _seed(conn):
    conn.execute("INSERT INTO accounts(handle, school) VALUES('a','Macro')")
    rows = [(1, "BUY", "BUY", "CORRECT"), (2, "SELL", "BUY", "WRONG"), (3, "BUY", "NEUTRAL", "PARTIAL"), (4, "SELL", "SELL", "CORRECT")]
    for cid, d, actual, res in rows:
        conn.execute("INSERT INTO tweets(tweet_id, handle, created_at, text, source) VALUES(?,?,?,?,?)",
                     (f"t{cid}", "a", "2025-01-01T00:00:00+00:00", "x", "csv"))
        conn.execute("INSERT INTO calls(id, tweet_id, handle, asset, direction, horizon, confidence, called_at, model) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (cid, f"t{cid}", "a", "BTC", d, "SHORT", 0.9, "2025-01-01T00:00:00+00:00", "m"))
        conn.execute("INSERT INTO outcomes(call_id, entry_date, exit_date, entry_close, exit_close, return_pct, threshold_pct, "
                     "actual, result, evaluated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (cid, "2025-01-01", "2025-04-01", 100, 110, 10, 5, actual, res, "2025-04-01"))
    conn.commit()


def test_hit_rates_vs_always_buy(tmp_path):
    conn = db.connect_sqlite(tmp_path / "t.db")
    _seed(conn)
    hr = score.hit_rates(conn, "m")
    s = hr["SHORT"]
    assert s["n"] == 4
    assert s["rate"] == 0.625          # (1 + 0 + 0.5 + 1) / 4
    assert s["baseline"] == 0.625      # always-BUY: (1 + 1 + 0.5 + 0) / 4
    assert set(hr) == {"SHORT"}
