"""One-off: re-grade stored outcomes with `evaluate.grade` — NEUTRAL calls that the market falsified (moved beyond the
band either way) become WRONG instead of PARTIAL.

    PYTHONPATH=. .venv/bin/python scripts/migrate_neutral_results.py [--yes]

Without --yes it only reports. Re-run `python -m src.score` (or src.run) afterwards to recompute trust.
"""
import sys

from src.db import connect, log
from src.evaluate import grade

conn = connect()
rows = conn.execute("""SELECT o.call_id, c.model, c.direction, o.actual, o.result
                       FROM outcomes o JOIN calls c ON c.id=o.call_id""").fetchall()
changes = [(grade(r["direction"], r["actual"]), r["call_id"]) for r in rows
           if grade(r["direction"], r["actual"]) != r["result"]]
by_model: dict = {}
for r in rows:
    if grade(r["direction"], r["actual"]) != r["result"]:
        k = (r["model"], r["result"], grade(r["direction"], r["actual"]))
        by_model[k] = by_model.get(k, 0) + 1
print(f"{len(rows)} outcomes, {len(changes)} change result")
for k, n in sorted(by_model.items()):
    print(f"  {k[0]:40} {k[1]:8} → {k[2]:8} {n}")
if "--yes" in sys.argv and changes:
    conn.executemany("UPDATE outcomes SET result=? WHERE call_id=?", changes)
    conn.commit()
    log(f"migrate_neutral_results: re-graded {len(changes)} outcomes")
    print("updated", len(changes))
