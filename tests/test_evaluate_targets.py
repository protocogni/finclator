"""Price-target credit: a target on the wrong side of the entry (or a unit error) must not earn ±0.25."""
from datetime import date

from src import db, evaluate
from src.evaluate import grade, target_is_sane


def test_grade_neutral_is_not_free():
    assert grade("BUY", "BUY") == "CORRECT"
    assert grade("BUY", "NEUTRAL") == "PARTIAL"
    assert grade("BUY", "SELL") == "WRONG"
    assert grade("NEUTRAL", "NEUTRAL") == "CORRECT"
    assert grade("NEUTRAL", "BUY") == "WRONG"    # "sideways" falsified by a big move — was PARTIAL (0.5 floor)
    assert grade("NEUTRAL", "SELL") == "WRONG"


def test_target_is_sane():
    assert target_is_sane("BUY", 100_000, 66_000, "BTC")
    assert target_is_sane("SELL", 60_000, 66_000, "BTC")
    assert target_is_sane("BUY", 1_000_000, 61_198, "BTC")     # "$BTC = $1mm": hyperbole, but a real claim (16×)
    assert target_is_sane("SELL", 10_000, 84_353, "BTC")       # "ride Bitcoin down to $10K"
    assert not target_is_sane("BUY", 8_000, 66_222, "BTC")     # "Bitcoin to $8'000!!!?" labeled BUY → hit by construction
    assert not target_is_sane("SELL", 78_000, 69_927, "BTC")   # "one final rally towards $78k" labeled SELL
    assert not target_is_sane("BUY", 285, 4_644, "GOLD")       # gold unit error (ratio 0.06)
    assert not target_is_sane("SELL", 76, 4_720, "GOLD")       # gold unit error (0.016)
    assert not target_is_sane("SELL", 485, 4_846, "SPX")       # SPY units against SPX (0.10)
    assert not target_is_sane("BUY", 50_000, 6_932, "SPX")     # "DOW 50,000" against SPX (7.2)
    assert not target_is_sane("BUY", 100_000_000, 70_745, "BTC")  # 1,413×: not a claim
    assert not target_is_sane("BUY", 66_000, 66_000, "BTC")    # equal to entry: no claim
    assert not target_is_sane("BUY", 0, 66_000, "BTC")
    assert not target_is_sane("BUY", 100, 0, "BTC")


def test_wrong_side_target_gets_no_credit(tmp_path):
    conn = db.connect_sqlite(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts(handle, school) VALUES('a', 'Macro')")
    d = date(2024, 1, 5)
    for i in range(0, 120):
        day = date.fromordinal(d.toordinal() + i).isoformat()
        conn.execute("INSERT INTO prices(asset, date, close) VALUES('BTC', ?, ?)", (day, 40_000 + 50 * i))
    conn.execute("INSERT INTO tweets(tweet_id, handle, created_at, text, source) VALUES('t1','a',?, 'x', 'csv')", (d.isoformat(),))
    conn.execute("INSERT INTO tweets(tweet_id, handle, created_at, text, source) VALUES('t2','a',?, 'x', 'csv')", (d.isoformat(),))
    # BUY with a target BELOW entry (40,000): old code marked it HIT because max(close) ≥ entry ≥ target
    conn.execute("INSERT INTO calls(tweet_id, handle, asset, direction, horizon, confidence, price_target, called_at, model) "
                 "VALUES('t1','a','BTC','BUY','SHORT',0.8,30000,?, 'm')", (d.isoformat() + "T00:00:00+00:00",))
    # BUY with a sane target above entry, reached inside the window
    conn.execute("INSERT INTO calls(tweet_id, handle, asset, direction, horizon, confidence, price_target, called_at, model) "
                 "VALUES('t2','a','BTC','BUY','SHORT',0.8,42000,?, 'm')", (d.isoformat() + "T00:00:00+00:00",))
    conn.commit()
    assert evaluate.evaluate(conn, today=date(2024, 6, 1)) == 2
    rows = {r["tweet_id"]: r for r in conn.execute(
        "SELECT c.tweet_id, o.target_hit, o.extreme FROM outcomes o JOIN calls c ON c.id=o.call_id")}
    assert rows["t1"]["target_hit"] is None and rows["t1"]["extreme"] is None
    assert rows["t2"]["target_hit"] == 1 and rows["t2"]["extreme"] >= 42000
