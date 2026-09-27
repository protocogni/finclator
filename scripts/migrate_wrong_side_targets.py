"""One-off: null `target_hit`/`extreme` on stored outcomes whose price target fails `evaluate.target_is_sane`
(wrong side of the entry close, or an implausible ratio). Those targets were hit by construction and earned +0.25.

    PYTHONPATH=. .venv/bin/python scripts/migrate_wrong_side_targets.py [--yes]

Without --yes it only reports. Re-run `python -m src.score` (or src.run) afterwards to recompute trust.
"""
import sys

from src.db import connect, log
from src.evaluate import target_is_sane

conn = connect()
rows = conn.execute("""SELECT o.call_id, c.model, c.handle, c.asset, c.direction, c.price_target, o.entry_close, o.target_hit
                       FROM outcomes o JOIN calls c ON c.id=o.call_id
                       WHERE c.price_target IS NOT NULL AND o.target_hit IS NOT NULL""").fetchall()
bad = [r for r in rows if not target_is_sane(r["direction"], r["price_target"], r["entry_close"], r["asset"])]
by_model: dict = {}
for r in bad:
    k = (r["model"], r["asset"], r["direction"])
    by_model[k] = by_model.get(k, 0) + 1
print(f"{len(rows)} outcomes with a target, {len(bad)} insane (hit={sum(r['target_hit'] for r in bad)})")
for k, n in sorted(by_model.items()):
    print(f"  {k[0]:40} {k[1]:5} {k[2]:5} {n}")
for r in bad[:12]:
    print(f"  e.g. @{r['handle']} {r['asset']} {r['direction']} target {r['price_target']:,.0f} @ entry {r['entry_close']:,.0f}")
if "--yes" in sys.argv and bad:
    ids = [r["call_id"] for r in bad]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(f"UPDATE outcomes SET target_hit=NULL, extreme=NULL WHERE call_id IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
    log(f"migrate_wrong_side_targets: nulled target_hit on {len(ids)} outcomes")
    print("updated", len(ids))
