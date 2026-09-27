"""Public-site data: writes public/site.json from the DB + matrix.json (numbers on the landing page are never typed by hand).

Runs at the end of src.run (after matrix) and can be run alone: `python -m src.site`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import score
from .db import connect
from .models import active_model

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "site.json"


def build(conn: sqlite3.Connection, model: str | None = None) -> dict:
    model = model or active_model()
    q = lambda s, *a: conn.execute(s, a).fetchone()  # noqa: E731
    f = q("""SELECT (SELECT count(*) FROM accounts) accounts,
                    (SELECT count(*) FROM tweets) tweets,
                    (SELECT min(created_at) FROM tweets) t0, (SELECT max(created_at) FROM tweets) t1,
                    (SELECT count(*) FROM calls WHERE model=?) calls,
                    (SELECT count(*) FROM outcomes o JOIN calls k ON k.id=o.call_id WHERE k.model=?) outcomes,
                    (SELECT count(*) FROM trust WHERE model=? AND asset='*' AND horizon='*' AND n>=20) scored20""",
          model, model, model)
    hit = score.hit_rates(conn, model)  # {horizon: {rate, baseline (always-BUY on the same outcomes), n}}
    results = {r["result"]: r["n"] for r in conn.execute(
        "SELECT o.result, count(*) n FROM outcomes o JOIN calls k ON k.id=o.call_id WHERE k.model=? GROUP BY 1", (model,))}
    spread = q("SELECT min(score) lo, max(score) hi FROM trust WHERE model=? AND asset='*' AND horizon='*' AND n>=20", model)
    schools = [{"school": r["school"], "n": r["n"]} for r in conn.execute(
        "SELECT school, count(*) n FROM accounts GROUP BY 1 ORDER BY 2 DESC")]
    # a real, verifiable, matured example with a stated target: the most recent CORRECT one with a short quote
    ex = q("""SELECT k.handle, k.asset, k.direction, k.horizon, substr(k.called_at,1,10) called, k.price_target, k.quote,
                     o.entry_date, o.exit_date, o.entry_close, o.exit_close, o.return_pct, o.threshold_pct, o.result, k.tweet_id
              FROM calls k JOIN outcomes o ON o.call_id=k.id JOIN tweets t ON t.id=k.tweet_id
              WHERE k.model=? AND o.result='CORRECT' AND k.price_target IS NOT NULL AND length(k.quote) BETWEEN 30 AND 120
                AND instr(t.text, k.quote) > 0 AND k.horizon='SHORT' AND k.quote LIKE '%$%' AND t.lang='en'
                AND k.quote NOT LIKE '%above%' AND k.quote NOT LIKE '%below%' AND k.quote NOT LIKE '%hold%'
              ORDER BY k.called_at DESC LIMIT 1""", model)
    matrix_p = ROOT / "data" / "matrix.json"
    m = json.loads(matrix_p.read_text()) if matrix_p.exists() else {"cells": {}}
    cells = {k: {"label": v["label"], "net": v["net"], "n": v["n_calls"], "accounts": v.get("n_accounts", v["n_calls"]),
                 "top_share": v.get("top_share", 0)}
             for k, v in m.get("cells", {}).items()}
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "accounts": f["accounts"], "tweets": f["tweets"], "from": f["t0"][:4], "to": f["t1"][:10],
        "calls": f["calls"], "outcomes": f["outcomes"], "results": results, "hit_by_horizon": hit,
        "scored_accounts": f["scored20"], "trust_lo": round(spread["lo"] or 0, 2), "trust_hi": round(spread["hi"] or 0, 2),
        "schools": schools, "matrix": cells, "matrix_generated_at": (m.get("generated_at") or "")[:10],
        "example": dict(ex) if ex else None,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    print(json.dumps(build(connect()), indent=1)[:600])
