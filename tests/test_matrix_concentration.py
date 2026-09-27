"""matrix.build: one vote per account per cell (latest call), top_handle/top_share, contributors ordered by weight."""
from datetime import date

from src import db, matrix


def _seed(conn, specs):
    """specs: (handle, direction, confidence, called_at) — no trust rows → 0.5 prior for everyone."""
    for h in {s[0] for s in specs}:
        conn.execute("INSERT INTO accounts(handle, school) VALUES(?, 'Macro')", (h,))
    for i, (h, d, conf, at) in enumerate(specs):
        tid = f"{h}{i}"
        conn.execute("INSERT INTO tweets(id, handle, created_at, text, source) VALUES(?,?,?,?,?)", (tid, h, at, "x", "csv"))
        conn.execute("INSERT INTO calls(tweet_id, handle, asset, direction, horizon, confidence, called_at, model) "
                     "VALUES(?,?,?,?,?,?,?,?)", (tid, h, "BTC", d, "SHORT", conf, at, "m"))
    conn.commit()


def test_top_share(tmp_path):
    conn = db.connect_sqlite(tmp_path / "t.db")
    d = "2026-09-20T00:00:00+00:00"
    _seed(conn, [("big", "BUY", 1.0, d)] * 3 + [("small", "BUY", 0.5, d)])
    m = matrix.build(conn, today=date(2026, 9, 21), write=False, model="m")
    c = m["cells"]["BTC:SHORT"]
    # big counts once (0.5 × 1.0), not three times
    assert c["top_handle"] == "big"
    assert abs(c["top_share"] - 0.5 / 0.75) < 1e-3
    assert c["n_calls"] == 4 and c["n_accounts"] == 2
    assert len(c["contributors"]) == 2
    assert c["contributors"][0]["handle"] == "big"
    assert c["contributors"][0]["weight"] >= c["contributors"][-1]["weight"]
    assert m["cells"]["GOLD:LONG"]["top_handle"] is None


def test_one_vote_per_account_uses_latest_call(tmp_path):
    """A prolific SELLer with three older calls cannot outvote two quieter BUYers; and its vote is its latest call."""
    conn = db.connect_sqlite(tmp_path / "t.db")
    _seed(conn, [("loud", "SELL", 1.0, "2026-09-10T00:00:00+00:00"),
                 ("loud", "SELL", 1.0, "2026-09-12T00:00:00+00:00"),
                 ("loud", "SELL", 1.0, "2026-09-14T00:00:00+00:00"),
                 ("loud", "BUY", 1.0, "2026-09-20T00:00:00+00:00"),   # flipped — this is the vote
                 ("q1", "BUY", 1.0, "2026-09-20T00:00:00+00:00"),
                 ("q2", "BUY", 1.0, "2026-09-20T00:00:00+00:00")])
    c = matrix.build(conn, today=date(2026, 9, 21), write=False, model="m")["cells"]["BTC:SHORT"]
    assert c["sell"] == 0.0
    assert c["label"] == "BUY"
    assert c["n_accounts"] == 3 and c["n_calls"] == 6
    assert [x["direction"] for x in c["contributors"]] == ["BUY"] * 3
    assert next(x for x in c["contributors"] if x["handle"] == "loud")["date"] == "2026-09-20"
