"""Nightly Neon backup: pg_dump (custom format) → data/backups/finclator_<UTC ts>.dump, keep the newest KEEP files.
Restore: pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" data/backups/<file>.dump
Usage: PYTHONPATH=. .venv/bin/python scripts/backup_db.py"""
import subprocess
import sys
import time
from pathlib import Path

from src.db import ROOT, database_url, log

KEEP = 14
PG_DUMP = next((p for p in ("/opt/homebrew/opt/libpq/bin/pg_dump", "/usr/local/opt/libpq/bin/pg_dump", "pg_dump")
                if p == "pg_dump" or Path(p).exists()), "pg_dump")   # brew libpq = v18 client; postgresql@16's refuses Neon 18

url = database_url()
if not url:
    sys.exit("no DATABASE_URL")
out_dir = ROOT / "data" / "backups"
out_dir.mkdir(parents=True, exist_ok=True)
out = out_dir / f"finclator_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.dump"
t = time.time()
r = subprocess.run([PG_DUMP, "-Fc", "-f", str(out), "--no-owner", "--no-privileges", "-n", "public", url],
                   capture_output=True, text=True)
if r.returncode:
    log(f"backup: FAILED {r.stderr.strip()[-300:]}")
    sys.exit(1)
old = sorted(out_dir.glob("finclator_*.dump"))[:-KEEP]
for p in old:
    p.unlink()
log(f"backup: {out.name} {out.stat().st_size / 1e6:.1f} MB in {time.time() - t:.0f}s (kept {KEEP}, removed {len(old)})")
