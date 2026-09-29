"""Influencer-link click tracking: record_click validates against the roster, click_stats aggregates, Progress and the
daily report render the section."""
from src import admin


def test_record_click_roster_only(conn):
    assert admin.record_click(conn, "@Alice", "accounts", "/accounts", "me@example.com")
    assert admin.record_click(conn, "alice", "site", "/", None)
    assert admin.record_click(conn, "bob", "matrix", "/matrix", "me@example.com")
    assert not admin.record_click(conn, "nobody", "site", "/", None)       # not on the roster
    assert not admin.record_click(conn, "", "site", "/", None)
    assert not admin.record_click(conn, "status", "site", "/", None)       # x.com/<handle>/status/<id> split junk
    s = admin.click_stats(conn)
    assert s["total"] == 3 and s["clickers"] == 1
    assert s["top"][0] == {"handle": "alice", "n": 2, "who": 1}
    assert s["top"][1] == {"handle": "bob", "n": 1, "who": 1}
    assert s["by_src"] == {"accounts": 1, "site": 1, "matrix": 1} or set(s["by_src"]) == {"accounts", "site", "matrix"}


def test_progress_shows_most_clicked(conn):
    html = admin.page_progress(conn)
    assert "Most clicked influencers" in html and "No clicks recorded yet" in html
    admin.record_click(conn, "alice", "accounts", "/accounts", "me@example.com")
    html = admin.page_progress(conn)
    assert "href='/accounts#acc-alice'" in html and "No clicks recorded yet" not in html


def test_page_carries_click_beacon(conn):
    html = admin._page("t", "<p>x</p>", "/accounts")
    assert 'CLICK_URL="/api/click"' in html and "sendBeacon(CLICK_URL" in html
    assert "<body data-tab='accounts'>" in html


def test_report_audience_and_skill_sections(conn, monkeypatch):
    from datetime import datetime, timezone

    from src import report
    admin.record_click(conn, "alice", "accounts", "/accounts", "me@example.com")
    now = datetime.now(timezone.utc)
    d = report.gather(conn, now, model="m")
    assert d["clicks"]["d7"]["total"] == 1 and d["clickers"][0]["who"] == "me@example.com"
    subject, html = report.render(d, None, None, [], {}, now)
    assert "Does the roster beat" in html and "roster right" in html
    assert "Most clicked influencers" in html and "@alice" in html and "me@example.com" in html
    assert "Least reliable" not in html          # fixture's only scored account has n=1 < 10 → lists stay empty
    conn.execute("UPDATE trust SET n=12 WHERE handle='alice' AND asset='*'")
    conn.commit()
    d = report.gather(conn, now, model="m")
    assert d["top_trust"][0]["handle"] == "alice" and d["bottom_trust"][0]["handle"] == "alice"
    _, html = report.render(d, None, None, [], {}, now)
    assert "Most reliable" in html and "Least reliable" in html
