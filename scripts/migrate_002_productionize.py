"""Back up Neon (pg_dump custom format → data/backup_<ts>.dump), then dry-run the structural steps in a transaction
that is rolled back, printing the resulting catalog. `--apply` runs them for real (connect() does it) and verifies."""
import subprocess
import sys
import time
from pathlib import Path

from src import db

url = db.database_url()
assert url, "no DATABASE_URL"
ROOT = Path("/Users/jakdemir/projects/finclator")

if "--apply" not in sys.argv and "--no-backup" not in sys.argv:
    out = ROOT / "data" / f"backup_{time.strftime('%Y%m%d_%H%M%S')}.dump"
    t = time.time()
    pg_dump = "/opt/homebrew/opt/libpq/bin/pg_dump"   # v18 client (brew libpq); postgresql@16's refuses Neon 18
    r = subprocess.run([pg_dump, "-Fc", "-f", str(out), "--no-owner", "--no-privileges", "-n", "public", url],
                       capture_output=True, text=True)
    print("pg_dump", r.returncode, r.stderr[-500:], f"{out.stat().st_size/1e6:.1f} MB" if out.exists() else "", f"{time.time()-t:.0f}s")
    if r.returncode:
        sys.exit(1)

counts_before = {}
c = db._PgConnection(url)
for t in ("accounts", "tweets", "classified_by", "gate", "calls", "prices", "outcomes", "trust", "clicks"):
    counts_before[t] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
print("before", counts_before)

if "--apply" in sys.argv:
    conn = db.connect()
else:
    # dry run: run every step inside one transaction, inspect, roll back
    conn = c
    for name, fn in db.STEPS:
        fn(conn)
        print("dry-ran", name)
    conn.execute("SAVEPOINT s")

for t in db.tables(conn):
    print(f"{t:16s} pk={sorted(db.primary_key(conn, t))} uniq={db.unique_keys(conn, t)}")
print("fks:", [(r[0], r[1]) for r in conn.execute(
    "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace ORDER BY 1")])
print("checks:", [r[0] for r in conn.execute(
    "SELECT conname FROM pg_constraint WHERE contype='c' AND connamespace='public'::regnamespace ORDER BY 1")])
after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts_before}
print("after ", after)
assert after == counts_before, "row counts changed!"
print("tweets sample:", tuple(conn.execute("SELECT id, tweet_id, handle FROM tweets ORDER BY id LIMIT 2").fetchall()[0]))
print("max ids:", {t: conn.execute(f"SELECT max(id) FROM {t}").fetchone()[0] for t in counts_before})
if "--apply" in sys.argv:
    print("schema_migrations:", [tuple(r) for r in conn.execute("SELECT name, applied_at FROM schema_migrations")])
    print("reports:", [tuple(r)[:4] for r in conn.execute("SELECT id, at, subject, labels FROM reports")])
    from src import matrix
    m = matrix.build(conn, write=False)
    print("matrix:", {k: v["label"] for k, v in m["cells"].items()})
else:
    conn.rollback()
    print("rolled back")
