"""Every Jev@0.3 extra (tweet, asset) call that Fable did not make, grouped by what Opus and Qwen3.6 said about it."""
import json
from collections import Counter

ASSETS = ("BTC", "GOLD", "SPX")
D = {"up": "BUY", "down": "SELL", "neutral": "NEUTRAL"}
R = "data/"


def load(*paths):
    out = {}
    for p in paths:
        out.update({json.loads(ln)["id"]: json.loads(ln) for ln in open(p) if ln.strip()})
    return out


def calls_of(rec):
    if not rec or not rec.get("is_call"):
        return {}
    return {c["asset"]: (c["direction"], c["horizon"]) for c in rec.get("calls", []) if c.get("asset") in ASSETS}


pending = load(R + "pending_en1000.jsonl")
fable = load(R + "labels_en1000_fable51.jsonl")
opus = load(R + "labels_en_opus55.jsonl")
jev = load(R + "jev_raw_en.jsonl", R + "jev_raw_en2.jsonl")
q36 = load(R + "qwen_preds_en.jsonl", R + "qwen_preds_en2.jsonl")

extras = []
for i, r in jev.items():
    a = r["raw"]["answers"]
    if a["is_call"]["noul"] < 0.3:
        continue
    f = calls_of(fable[i])
    for x in ASSETS:
        st = a.get(f"stance_{x}")
        if st and st["choice"] != "none" and x not in f:
            extras.append((i, x, a["is_call"]["noul"], D[st["choice"]], a.get(f"horizon_{x}", {}).get("choice")))

print(f"Jev@0.3 extras (Jev called, Fable did not): {len(extras)}\n")
groups = Counter()
for i, x, p, d, h in extras:
    o = calls_of(opus[i]).get(x)
    q = calls_of(q36[i]["pred"]).get(x)
    fab_other = [k for k in calls_of(fable[i]) if k != x]
    key = ("Opus agrees with Jev" if o else "Opus: not a call") + " / " + ("Qwen agrees" if q else "Qwen: not a call")
    if fab_other:
        key += " / Fable called a DIFFERENT asset on this tweet"
    groups[key] += 1
for k, n in groups.most_common():
    print(f"  {n:>3}  {k}")

print("\n--- the 55, one line each: p_call | Jev label | Opus | Qwen | text ---")
for i, x, p, d, h in sorted(extras, key=lambda e: -e[2]):
    o = calls_of(opus[i]).get(x)
    q = calls_of(q36[i]["pred"]).get(x)
    fo = calls_of(fable[i])
    fab = f"fable={list(fo)}" if fo else "fable=none"
    print(f"{p:.2f} | {x} {d} {h or '?':<6} | opus={'/'.join(o) if o else '-':<12} | qwen={'/'.join(q) if q else '-':<12} | {fab:<18} | "
          f"{pending[i]['text'][:130].replace(chr(10), ' ')!r}")
