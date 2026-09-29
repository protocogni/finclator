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
    assert report.load_state(conn) == {}
    report.save_state(conn, {"labels": {"BTC:SHORT": "BUY"}})
    assert report.load_state(conn)["labels"]["BTC:SHORT"] == "BUY"
    report.save_state(conn, {"labels": {"BTC:SHORT": "SELL"}})
    assert report.load_state(conn)["labels"]["BTC:SHORT"] == "SELL"


def test_run_dry_does_not_persist(conn, monkeypatch):
    monkeypatch.setattr(report, "vercel_analytics", lambda now: None)
    monkeypatch.setattr(report, "vendor_credits", lambda: None)
    out = report.run(conn, dry=True)
    assert "html" in out and out["sent"] is None
    assert report.load_state(conn) == {}
