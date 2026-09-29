"""Zero-cost screener: regex for directional / advisory vocabulary (EN+TR). Reports recall vs gold and pass-through."""
import json
import re
import sys

from src.db import connect

gold_path = sys.argv[1] if len(sys.argv) > 1 else "data/labels_backfill.jsonl"
gold = {json.loads(l)["id"]: json.loads(l) for l in open(gold_path)}
rows = connect().execute("SELECT tweet_id AS id, text FROM tweets WHERE tweet_id IN (%s)" % ",".join("?" * len(gold)), list(gold)).fetchall()

DIRECTIONAL = re.compile(r"""(
 bull|bear|long\b|short\b|buy|sell|hold|hodl|dip|rally|rip|moon|crash|dump|pump|breakout|breakdown|target|\btp\b|
 support|resistance|bottom|top\b|peak|higher|lower|upside|downside|bubble|overvalued|undervalued|cheap|expensive|
 expect|will\b|should|going to|gonna|likely|could|may\b|might|soon|next|coming|
 al[ıi]?n?\b|al[ıi]m|sat[ıi]?n?\b|sat[ıi]ş|tut|bekl|hedef|destek|direnç|dip\b|zirve|tepe|yüksel|düş|çök|patla|
 balon|pahal[ıi]|ucuz|değerli|boğa|ay[ıi]\b|yukar[ıi]|aşağ[ıi]|görece|gider|gelir|olacak|olur|sürer|devam|
 kar\b|kâr|stop|pozisyon|position|vade|term|cycle|döngü|trend|\d+[kK]\b|\$\s?\d{2,}|\d{2,}\s?(k|bin)\b
)""", re.IGNORECASE | re.VERBOSE)

tp = fn = kept = 0
missed = []
for r in rows:
    keep = bool(DIRECTIONAL.search(r["text"]))
    g = gold[r["id"]]["is_call"]
    kept += keep
    if g and keep:
        tp += 1
    elif g:
        fn += 1
        missed.append(r["text"][:100].replace("\n", " "))
print(f"regex: n={len(rows)} recall={tp}/{tp+fn}={tp/max(1,tp+fn):.1%} pass-through={kept}/{len(rows)}={kept/len(rows):.0%}")
for m in missed:
    print("  MISSED:", m)
