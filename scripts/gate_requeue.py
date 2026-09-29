"""Gate rows between the old (0.3) and new (0.2) threshold: tweets stored as non-calls that would now pass.
Report only; optionally --requeue deletes their classified_by row for the active model so the next classify_pending
sends them through Qwen (they have no calls rows, so nothing else to undo)."""
import json
import sys

from src.db import connect, log
from src.gate import THRESHOLD, passes
from src.models import active_model

OLD = 0.3
conn = connect()
model = active_model()
rows = conn.execute("SELECT tweet_id, p_call, stances FROM gate WHERE p_call >= ? AND p_call < ?", (THRESHOLD, OLD)).fetchall()
flip = [r for r in rows if passes(r["p_call"], json.loads(r["stances"] or "{}"))]
ids = [r["tweet_id"] for r in flip]
total = conn.execute("SELECT count(*) FROM gate").fetchone()[0]
print(f"gate rows: {total:,}; in [{THRESHOLD}, {OLD}): {len(rows):,}; of those passing the stance rule: {len(flip):,}")
if ids:
    ph = ",".join("?" * len(ids))
    stored = conn.execute(f"SELECT count(*) FROM classified_by WHERE model=? AND tweet_id IN ({ph})", [model, *ids]).fetchone()[0]
    with_calls = conn.execute(f"SELECT count(DISTINCT tweet_id) FROM calls WHERE model=? AND tweet_id IN ({ph})",
                              [model, *ids]).fetchone()[0]
    print(f"  stored as classified under {model}: {stored:,}; of those already having calls rows: {with_calls:,}")
    if "--requeue" in sys.argv:
        n = conn.execute(f"DELETE FROM classified_by WHERE model=? AND tweet_id IN ({ph}) "
                         f"AND tweet_id NOT IN (SELECT tweet_id FROM calls WHERE model=?)", [model, *ids, model]).rowcount
        conn.commit()
        log(f"gate threshold {OLD}→{THRESHOLD}: requeued {n} tweets for {model} (classified_by rows removed)")
