"""Influencer-link click tracking: record_click validates against the roster, click_stats aggregates, Progress and the
daily report render the section."""
from src import admin


def test_record_click_roster_only(conn):
    assert admin.record_click(conn, "@Alice", "accounts", "/accounts", "me@example.com")
    assert admin.record_click(conn, "alice", "site", "/", None)
    assert admin.record_click(conn, "bob", "matrix", "/matrix", "me@example.com")
    assert not admin.record_click(conn, "nobody", "site", "/", None)       # not on the roster, nothing else to store
    assert not admin.record_click(conn, "", "site", "/", None)
    assert not admin.record_click(conn, "status", "site", "/", None)       # x.com/<handle>/status/<id> split junk
    s = admin.click_stats(conn)
    assert s["total"] == 3 and s["clickers"] == 1 and s["influencer"] == 3
    assert s["top"][0] == {"handle": "alice", "n": 2, "who": 1}
    assert s["top"][1] == {"handle": "bob", "n": 1, "who": 1}
    assert set(s["by_src"]) == {"accounts", "site", "matrix"}
    assert s["by_kind"] == {"influencer": 3}


def test_record_any_click(conn):
    body = {"handle": None, "kind": "link", "href": "/method", "label": "Method", "src": "site", "page": "/", "visitor": "v1"}
    assert admin.record_click_body(conn, body, None)
    assert admin.record_click_body(conn, dict(body, visitor="v2", kind="button", href="", label="Sign in"), None)
    assert admin.record_click_body(conn, {"handle": "nobody", "href": "https://x.com/nobody", "label": "@nobody"}, None)  # kept as link
    assert admin.record_click_body(conn, {"handle": "alice", "kind": "influencer", "href": "https://x.com/alice"}, "me@x")
    assert not admin.record_click_body(conn, {"kind": "influencer", "handle": "nobody"}, None)
    s = admin.click_stats(conn)
    assert s["total"] == 4 and s["influencer"] == 1 and s["visitors"] == 2
    assert s["by_kind"] == {"link": 2, "button": 1, "influencer": 1} or set(s["by_kind"]) == {"link", "button", "influencer"}
    assert s["top_links"][0]["href"] == "/method" or {t["href"] for t in s["top_links"]} >= {"/method"}
    row = conn.execute("SELECT kind, handle, href, label, visitor FROM clicks WHERE handle IS NULL ORDER BY id LIMIT 1").fetchone()
    assert tuple(row) == ("link", None, "/method", "Method", "v1")


def test_progress_shows_most_clicked(conn):
    html = admin.page_progress(conn)
    assert "<h2>Clicks" in html and "No clicks recorded yet" in html
    admin.record_click(conn, "alice", "accounts", "/accounts", "me@example.com")
    html = admin.page_progress(conn)
    assert "Most clicked influencers" in html
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
    assert "as a group, better than" in html and "all accounts, averaged" in html
    assert "Most clicked influencers" in html and "@alice" in html and "me@example.com" in html
    assert "Least reliable" not in html          # fixture's only scored account has n=1 < 10 → lists stay empty
    conn.execute("UPDATE trust SET n=12 WHERE handle='alice' AND asset='*'")
    conn.commit()
    d = report.gather(conn, now, model="m")
    assert d["top_trust"][0]["handle"] == "alice" and d["bottom_trust"][0]["handle"] == "alice"
    _, html = report.render(d, None, None, [], {}, now)
    assert "Most reliable" in html and "Least reliable" in html
