"""Audit tab rendering against the in-memory fixture (see conftest.py)."""
from src import audit


def test_audit_clamps_long_tweets(conn):
    conn.execute("UPDATE tweets SET text=? WHERE tweet_id='3'", ("x " * 3000,))
    conn.commit()
    html = audit.body(conn, model="m")
    assert html.count("class='tweet clamp'") == 1          # only the long one
    assert ".tweet.clamp{max-height:" in audit.CSS
    assert "function unclamp" in audit.JS


def test_audit_shows_gate_probability(conn):
    html = audit.body(conn, model="m")
    assert "<th title='Jev gate p_call" in html
    assert "<td class=num>0.90</td>\n<td class=num>0.95</td>" in html          # call 1: conf then gate
    assert "<td class=num>0.60</td>\n<td class=num><small>–</small></td>" in html   # call 2, pre-gate
    assert "<td colspan=15>" in html and "<td colspan=14>" not in html
