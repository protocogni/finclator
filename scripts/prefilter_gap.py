"""Count English prefilter-rejected originals containing candidate expansion terms; show gate hit-rate per term
from data/prefilter_rejects_jev.jsonl. Read-only."""
import json
import re
from collections import Counter

from src.db import connect

TERMS = {
    "stocks": r"\bstocks?\b", "stock market": r"\bstock ?market\b", "equities": r"\bequit(y|ies)\b",
    "the market": r"\bthe market\b", "bull/bear market": r"\b(bull|bear) market\b", "risk assets": r"\brisk assets?\b",
    "crypto": r"\bcrypto\w*", "₿": "₿", "indices/index": r"\bindic?es\b|\bindex\b", "recession": r"\brecession\b",
    "tech": r"\btech\b", "SPX-like level": r"\b[4-7],?\d{3}\b",
}
TR = re.compile(r"[ğışçöüİĞŞÇÖÜ]")
conn = connect()
rows = conn.execute("SELECT t.text, a.language FROM tweets t JOIN accounts a ON a.handle=t.handle "
                    "WHERE t.relevant=0 AND t.is_reply=0").fetchall()
en = [r["text"] for r in rows if not TR.search(r["text"]) and (r["language"] or "en") != "tr"]
print(f"EN rejected originals: {len(en):,}")
sample = [json.loads(ln) for ln in open("data/prefilter_rejects_jev.jsonl")]
print(f"{'term':18} {'rejected':>9} {'in sample':>9} {'gate pass':>9}")
for name, pat in TERMS.items():
    rx = re.compile(pat, re.I)
    n_all = sum(1 for t in en if rx.search(t))
    s = [r for r in sample if rx.search(r["text"])]
    hit = sum(1 for r in s if r["pred"]["is_call"])
    print(f"{name:18} {n_all:>9,} {len(s):>9} {hit:>9}")
any_rx = re.compile("|".join(f"(?:{p})" for p in TERMS.values()), re.I)
s = [r for r in sample if any_rx.search(r["text"])]
print(f"\nany term: {sum(1 for t in en if any_rx.search(t)):,} rejected; sample {len(s)}, gate pass "
      f"{sum(1 for r in s if r['pred']['is_call'])} of the 32 total")
print("gate passes with NO term:", [r["text"][:80] for r in sample if r["pred"]["is_call"] and not any_rx.search(r["text"])])
print(Counter(r["handle"] for r in sample if r["pred"]["is_call"]).most_common(8))
