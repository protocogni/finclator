"""Daily ops report renders against the shared fixture, flags stale state, and tracks matrix label deltas."""
from datetime import datetime, timezone

from src import report


def test_gather_and_render(conn, monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(report, "vercel_analytics", lambda now: None)
    monkeypatch.setattr(report, "vendor_credits", lambda: 1.0)
    d = report.gather(conn, now, model="m")
    assert d["funnel"]["calls"] == 2 and d["funnel"]["outcomes"] == 1 and d["funnel"]["pending"] == 0
    assert d["activity"]["gated"]["now"] == 1  # gate row at 2026-09-23 is inside the 24 h window
    assert "MEDIUM" in d["hit_rates"]
    P = report.problems(d, None, 1.0, now)
    texts = " | ".join(t for _, t in P)
    assert "newest stored tweet" in texts        # fixture's newest tweet is weeks old
    assert "balance $1.00" in texts
    icon, word = report.verdict(P)
    assert icon == "🔴"
    subject, html = report.render(d, None, 1.0, P, {"labels": {"BTC:SHORT": "SELL"}}, now)
    assert subject.startswith("🔴 finclator daily")
    assert "Problems" in html and "Web Analytics unavailable" in html
    assert "First report" not in html
    assert "Traceback" not in html and "None" not in html.replace("None.", "")


def test_state_roundtrip(conn):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert report.load_state(conn) == {}
    i1 = report.save_report(conn, now, "s", ["a@b"], {"id": 1}, [], {"BTC:SHORT": "BUY"}, "<p>x</p>")
    assert report.load_state(conn)["labels"]["BTC:SHORT"] == "BUY"
    i2 = report.save_report(conn, now, "s", ["a@b"], {"id": 2}, ["p"], {"BTC:SHORT": "SELL"}, "<p>y</p>")
    assert i2 > i1
    assert report.load_state(conn)["labels"]["BTC:SHORT"] == "SELL"
    assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 2


def test_run_sends_and_persists(conn, monkeypatch):
    monkeypatch.setattr(report, "vercel_analytics", lambda now: None)
    monkeypatch.setattr(report, "vendor_credits", lambda: None)
    monkeypatch.setattr(report, "recipients", lambda: ["me@example.com"])
    monkeypatch.setattr(report, "send_email", lambda to, s, h: {"id": "mail-1"})
    out = report.run(conn, dry=False)
    assert out["sent"] == {"id": "mail-1"} and out["report_id"] == 1
    r = conn.execute("SELECT subject, recipients, html FROM reports").fetchone()
    assert r[0] == out["subject"] and "me@example.com" in r[1] and "<" in r[2]


def test_run_dry_does_not_persist(conn, monkeypatch):
    monkeypatch.setattr(report, "vercel_analytics", lambda now: None)
    monkeypatch.setattr(report, "vendor_credits", lambda: None)
    out = report.run(conn, dry=True)
    assert "html" in out and out["sent"] is None
    assert report.load_state(conn) == {}
