"""Re-run the prefilter over all stored tweets (after changing prefilter rules). Never touches classified tweets' calls;
newly-relevant tweets become pending for the classifier."""
from src.classify import pending
from src.db import connect, log
from src.models import active_model
from src.prefilter import is_relevant

conn = connect()
rows = conn.execute("SELECT tweet_id AS id, text, relevant, assets_hint FROM tweets").fetchall()
changed = newly = dropped = 0
for r in rows:
    rel, assets = is_relevant(r["text"], False)
    hint = ",".join(assets)
    if int(rel) != r["relevant"] or hint != r["assets_hint"]:
        conn.execute("UPDATE tweets SET relevant=?, assets_hint=? WHERE tweet_id=?", (int(rel), hint, r["id"]))
        changed += 1
        if rel and not r["relevant"]:
            newly += 1
        elif r["relevant"] and not rel:
            dropped += 1
conn.commit()
tot, rel = conn.execute("SELECT count(*), sum(relevant) FROM tweets").fetchone()
pend = len(pending(conn, None, active_model()))
log(f"reprefilter: {len(rows)} tweets, {changed} changed, {newly} newly relevant, {dropped} no longer relevant → "
    f"{rel}/{tot} relevant, {pend} pending for {active_model()}")
for r in conn.execute("SELECT assets_hint, count(*) FROM tweets WHERE relevant=1 GROUP BY 1 ORDER BY 2 DESC"):
    print("  ", tuple(r))
