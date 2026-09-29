"""Sample N English relevant tweets, ≤ K per account, spread over years → pending JSONL
(export format: id, handle, created_at, assets_hint, text). Excludes every id in the gold sets and in any
--exclude file. Read-only on the DB.
Usage: PYTHONPATH=. .venv/bin/python scripts/sample_tweets.py N K OUT [--exclude f.jsonl ...] [--seed S]"""
import json
import random
import re
import sys
from collections import Counter, defaultdict

from src.db import connect

args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(args[0]) if args else 400
K = int(args[1]) if len(args) > 1 else 8
OUT = args[2] if len(args) > 2 else "data/pending_en.jsonl"
exclude_files: list[str] = []
if "--exclude" in sys.argv:
    for a in sys.argv[sys.argv.index("--exclude") + 1:]:
        if a.startswith("--"):
            break
        exclude_files.append(a)
SEED = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 20260929
TR_CHARS = re.compile(r"[ğışçöüİĞŞÇÖÜ]")
TR_WORDS = re.compile(r"\b(ve|bir|için|bu|ile|çok|daha|gibi|kadar|ama|ancak|var|yok|olan|olarak|değil|sonra|önce|"
                      r"altın|dolar|borsa|hisse|yükseliş|düşüş)\b", re.I)


def is_tr(text: str) -> bool:
    return bool(TR_CHARS.search(text)) or len(TR_WORDS.findall(text)) >= 2


skip = set()
for p in ("data/labels_backfill.jsonl", "data/labels_holdout.jsonl", *exclude_files):
    skip |= {json.loads(ln)["id"] for ln in open(p) if ln.strip()}

conn = connect()
rows = conn.execute("SELECT t.tweet_id AS id, t.handle, t.created_at, t.assets_hint, t.text, a.language FROM tweets t "
                    "JOIN accounts a ON a.handle=t.handle WHERE t.relevant=1").fetchall()
print("relevant originals:", len(rows), "| account language values:", Counter(r["language"] for r in rows).most_common(5))
en = [r for r in rows if r["id"] not in skip and not is_tr(r["text"]) and (r["language"] or "en") != "tr"
      and 30 <= len(r["text"]) <= 1200]
print(f"EN candidates: {len(en)} (excluded {len(skip)} ids from {2 + len(exclude_files)} files)")
random.seed(SEED)
random.shuffle(en)
per_acc: dict[str, int] = defaultdict(int)
per_year: dict[str, int] = defaultdict(int)
cap_year = N // 4 + 20
picked = []
for r in en:
    y = r["created_at"][:4]
    if per_acc[r["handle"]] >= K or per_year[y] >= cap_year:
        continue
    per_acc[r["handle"]] += 1
    per_year[y] += 1
    picked.append(r)
    if len(picked) >= N:
        break
picked.sort(key=lambda r: r["id"])
with open(OUT, "w") as f:
    for r in picked:
        f.write(json.dumps({"id": r["id"], "handle": r["handle"], "created_at": r["created_at"],
                            "assets_hint": r["assets_hint"], "text": r["text"]}, ensure_ascii=False) + "\n")
print(f"wrote {len(picked)} → {OUT}; accounts={len(per_acc)}; years={dict(sorted(per_year.items()))}")
print("assets_hint:", Counter(r["assets_hint"] for r in picked).most_common(6))
