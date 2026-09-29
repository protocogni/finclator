"""`runs` table: src.run records one row per pipeline run with per-stage results; Progress renders it."""
import json

from src import admin, run


def test_run_records_stages_and_status(conn):
    r = run.Run(conn, {"no_fetch": True})
    assert conn.execute("SELECT status FROM runs WHERE id=?", (r.id,)).fetchone()[0] == "running"
    r.stage("prices", {"BTC": 3})
    r.stage("evaluate", 0)
    r.finish()
    row = conn.execute("SELECT status, finished_at, stages, args FROM runs WHERE id=?", (r.id,)).fetchone()
    assert row[0] == "ok" and row[1]
    assert list(json.loads(row[2])) == ["prices", "evaluate"] and json.loads(row[2])["prices"]["result"] == {"BTC": 3}
    assert json.loads(row[3]) == {"no_fetch": True}
    r2 = run.Run(conn, {})
    r2.finish("RuntimeError: boom")
    assert conn.execute("SELECT status, error FROM runs WHERE id=?", (r2.id,)).fetchone()[1].startswith("RuntimeError")


def test_progress_lists_runs(conn):
    html = admin.page_progress(conn)
    assert "No runs recorded yet" in html
    r = run.Run(conn, {})
    r.stage("fetch", {"alice": 2})
    r.finish("ValueError: x")
    html = admin.page_progress(conn)
    assert "Pipeline runs" in html and "failed" in html and "ValueError: x" in html and "fetch" in html
