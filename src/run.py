"""Daily pipeline: fetch → prefilter_learn → classify → prices → evaluate → score → matrix → audit → pine → site.

Every run is a row in `runs` (started/finished, status, per-stage results as JSON) so the panel and the daily
report can show what happened without reading the log file.
"""
from __future__ import annotations

import json
import socket
import sys
import traceback
from datetime import datetime, timezone

from . import audit, classify, evaluate, fetch, matrix, pine, prefilter_learn, prices, score, site
from .db import connect, log


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Run:
    """Context for one pipeline run: `stage(name, result)` records a stage, exit marks ok/failed."""

    def __init__(self, conn, args: dict):
        self.conn, self.stages = conn, {}
        conn.execute("INSERT INTO runs(started_at, status, stages, host, args) VALUES (?, 'running', '{}', ?, ?)",
                     (_now(), socket.gethostname(), json.dumps(args)))
        self.id = conn.execute("SELECT max(id) FROM runs").fetchone()[0]
        conn.commit()

    def stage(self, name: str, result) -> None:
        self.stages[name] = {"at": _now(), "result": result}
        self.conn.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(self.stages, default=str), self.id))
        self.conn.commit()
        log(f"{name}: {result}")

    def finish(self, error: str | None = None) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
                          (_now(), "failed" if error else "ok", error, self.id))
        self.conn.commit()


def main(skip_fetch: bool = False, skip_classify: bool = False) -> None:
    conn = connect()
    run = Run(conn, {"no_fetch": skip_fetch, "no_classify": skip_classify})
    try:
        fetch.sync_roster(conn)
        if not skip_fetch:
            run.stage("fetch", fetch.fetch_all(conn))
        if not skip_classify:
            # probe regex-rejected tweets with the gate, rescue its passes, learn vocabulary from them (before classify,
            # so rescued and retagged tweets are classified in this same run); a failure here never blocks the run
            try:
                run.stage("prefilter_learn", prefilter_learn.run(conn))
            except Exception as e:  # noqa: BLE001
                run.stage("prefilter_learn", f"skipped: {type(e).__name__}: {str(e)[:200]}")
            run.stage("classify", classify.classify_pending(conn))
        run.stage("prices", prices.update_prices(conn))
        run.stage("evaluate", evaluate.evaluate(conn))
        run.stage("score", score.recompute(conn))
        m = matrix.build(conn)
        matrix.print_grid(m)
        run.stage("matrix", {k: v.get("label") for k, v in m.get("cells", {}).items()})
        run.stage("audit", str(audit.build()))
        run.stage("pine", str(pine.generate(conn)))
        site.build(conn)
        run.stage("site", "public/site.json")
        run.finish()
    except BaseException as e:
        run.finish(f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}")
        raise


if __name__ == "__main__":
    main(skip_fetch="--no-fetch" in sys.argv, skip_classify="--no-classify" in sys.argv)
