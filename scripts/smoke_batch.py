"""Smoke: run 8 relevant tweets through the production classify_pending path (batched) under a throwaway model tag,
then delete those rows. Proves batching end to end against local Ollama; touches no real model's labels."""
import os
import time

os.environ.setdefault("FINCLATOR_MODEL_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("FINCLATOR_BATCH_SIZE", "4")
os.environ.setdefault("FINCLATOR_WORKERS", "2")
from src import classify  # noqa: E402
from src.db import connect  # noqa: E402

TAG = "smoke-batch"
conn = connect()
rows = conn.execute("SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets WHERE relevant=1 "
                    "ORDER BY created_at DESC LIMIT 8").fetchall()
classify.pending = lambda c, limit, model: rows
real_batch = classify.make_batch_classifier
classify.make_batch_classifier = lambda: (real_batch()[0], TAG)
t0 = time.time()
print("result:", classify.classify_pending(conn), f"in {time.time() - t0:.1f}s (batch={classify.BATCH_SIZE})")
print("rows under tag:", conn.execute("SELECT count(*) FROM classified_by WHERE model=?", (TAG,)).fetchone()[0],
      "calls:", conn.execute("SELECT count(*) FROM calls WHERE model=?", (TAG,)).fetchone()[0])
conn.execute("DELETE FROM calls WHERE model=?", (TAG,))
conn.execute("DELETE FROM classified_by WHERE model=?", (TAG,))
conn.commit()
print("cleaned:", conn.execute("SELECT count(*) FROM classified_by WHERE model=?", (TAG,)).fetchone()[0])
