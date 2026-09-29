"""Four-way comparison against Fable 5.1 labels (the reference until a better one exists), per (tweet, asset) call.
Models: Opus 5.5, Jev (typed questions, several thresholds), Qwen3.6-35B (production), Qwen3-30B (previous local).
Also reports how each model's misses/extras overlap and a per-asset / per-year breakdown.
Usage: PYTHONPATH=. .venv/bin/python scripts/compare_four.py
Inputs (data/): labels_en1000_fable51.jsonl, labels_en_opus55.jsonl, jev_raw_{en,en2}.jsonl,
                qwen_preds_{en,en2}.jsonl, qwen30b_preds_{en,en2}.jsonl, pending_en1000.jsonl"""
import json
import os
import sys
from collections import Counter

ASSETS = ("BTC", "GOLD", "SPX")
D = {"up": "BUY", "down": "SELL", "neutral": "NEUTRAL"}
R = "data/"
THR = float(os.environ.get("JEV_THRESHOLD", "0.3"))


def load(path):
    return {json.loads(ln)["id"]: json.loads(ln) for ln in open(path) if ln.strip()} if os.path.exists(path) else {}


def load_many(*paths):
    out = {}
    for p in paths:
        out.update(load(p))
    return out


def calls_of(rec, tid):
    if not rec or not rec.get("is_call"):
        return {}
    return {(tid, c["asset"]): (c["direction"], c["horizon"]) for c in rec.get("calls", [])
            if c.get("asset") in ASSETS and c.get("direction") in ("BUY", "SELL", "NEUTRAL")
            and c.get("horizon") in ("SHORT", "MEDIUM", "LONG")}


def jev_calls(r, thr):
    a = r["raw"]["answers"]
    if a["is_call"]["noul"] < thr:
        return {}
    out = {}
    for x in ASSETS:
        st = a.get(f"stance_{x}")
        if st and st["choice"] != "none":
            out[(r["id"], x)] = (D[st["choice"]], a.get(f"horizon_{x}", {}).get("choice"))
    return out


def score(G, P):
    both = set(G) & set(P)
    n = len(both) or 1
    d = sum(G[k][0] == P[k][0] for k in both)
    h = sum(G[k][1] == P[k][1] for k in both)
    a = sum(G[k] == P[k] for k in both)
    rec = len(both) / (len(G) or 1)
    prec = len(both) / (len(P) or 1)
    f1 = 2 * rec * prec / (rec + prec) if rec + prec else 0
    return dict(gold=len(G), pred=len(P), both=len(both), rec=rec, prec=prec, f1=f1, dir=d / n, hor=h / n,
                cell=a / n, e2e_dir=d / (len(G) or 1), e2e_cell=a / (len(G) or 1))


def row(name, s):
    return (f"| {name} | {s['pred']} | {s['both']} | {s['rec']:.2f} | {s['prec']:.2f} | {s['f1']:.2f} | {s['dir']:.2f} | "
            f"{s['hor']:.2f} | {s['cell']:.2f} | {s['e2e_dir']:.2f} | {s['e2e_cell']:.2f} |")


HDR = ("| model | calls | shared | recall | precision | F1 | direction | horizon | cell | e2e dir | e2e cell |\n"
       "|---|---|---|---|---|---|---|---|---|---|---|")


def tweet_level(ref_is, pred_is, ids):
    n = len(ids)
    acc = sum(ref_is[i] == pred_is[i] for i in ids) / n
    tp = sum(ref_is[i] and pred_is[i] for i in ids)
    fp = sum(pred_is[i] and not ref_is[i] for i in ids)
    fn = sum(ref_is[i] and not pred_is[i] for i in ids)
    pr = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    return acc, pr, rc


def confusion(G, P, idx, labels):
    both = set(G) & set(P)
    c = Counter((G[k][idx], P[k][idx]) for k in both)
    lines = ["| ref \\ pred | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for g in labels:
        lines.append(f"| {g} | " + " | ".join(str(c[(g, p)]) for p in labels) + " |")
    return "\n".join(lines)


pending = load(R + "pending_en1000.jsonl")
fable = load(R + "labels_en1000_fable51.jsonl")
opus = load(R + "labels_en_opus55.jsonl")
jev = load_many(R + "jev_raw_en.jsonl", R + "jev_raw_en2.jsonl")
q36 = load_many(R + "qwen_preds_en.jsonl", R + "qwen_preds_en2.jsonl")
q30 = load_many(R + "qwen30b_preds_en1000.jsonl", R + "qwen30b_preds_en.jsonl", R + "qwen30b_preds_en2.jsonl")
SKIP30 = os.environ.get("SKIP_30B") == "1"
ids = [i for i in pending if i in fable and i in opus and i in jev and i in q36 and (SKIP30 or i in q30)]
print(f"tweets with every labeler: {len(ids)} (pending {len(pending)}, fable {len(fable)}, opus {len(opus)}, "
      f"jev {len(jev)}, qwen3.6 {len(q36)}, qwen3-30b {len(q30)})")
if len(ids) < len(pending):
    print("  (scoring the intersection only)")
if not ids:
    sys.exit(1)

G = {}
for i in ids:
    G.update(calls_of(fable[i], i))
print(f"\nReference (Fable 5.1): {sum(bool(fable[i]['is_call']) for i in ids)} call tweets "
      f"({sum(bool(fable[i]['is_call']) for i in ids) / len(ids):.0%}), {len(G)} (tweet, asset) calls; "
      f"direction {dict(Counter(v[0] for v in G.values()))}, horizon {dict(Counter(v[1] for v in G.values()))}, "
      f"asset {dict(Counter(k[1] for k in G))}")

preds = {"Opus 5.5": {}, "Qwen3.6-35B (prod)": {}}
if not SKIP30:
    preds["Qwen3-30B"] = {}
for i in ids:
    preds["Opus 5.5"].update(calls_of(opus[i], i))
    preds["Qwen3.6-35B (prod)"].update(calls_of(q36[i]["pred"], i))
    if not SKIP30:
        preds["Qwen3-30B"].update(calls_of(q30[i]["pred"], i))
for thr in (0.2, 0.3, 0.4, 0.5):
    P = {}
    for i in ids:
        P.update(jev_calls(jev[i], thr))
    preds[f"Jev p≥{thr}"] = P

print(f"\n## Per (tweet, asset) call, reference = Fable 5.1, {len(ids)} tweets, {len(G)} reference calls\n")
print(HDR)
for name, P in preds.items():
    print(row(name, score(G, P)))

print("\n## Tweet-level is_call\n")
print("| model | accuracy | precision | recall |\n|---|---|---|---|")
ref_is = {i: bool(fable[i]["is_call"]) for i in ids}
for name, P in preds.items():
    pred_is = {i: any(k[0] == i for k in P) for i in ids}
    acc, pr, rc = tweet_level(ref_is, pred_is, ids)
    print(f"| {name} | {acc:.3f} | {pr:.2f} | {rc:.2f} |")

main = {"Opus 5.5": preds["Opus 5.5"], f"Jev p≥{THR}": preds[f"Jev p≥{THR}"],
        "Qwen3.6-35B (prod)": preds["Qwen3.6-35B (prod)"]}
if not SKIP30:
    main["Qwen3-30B"] = preds["Qwen3-30B"]
print("\n## Confusion on shared calls (rows = Fable, columns = model)\n")
for name, P in main.items():
    print(f"### {name} — direction\n" + confusion(G, P, 0, ["BUY", "NEUTRAL", "SELL"]))
    print(f"\n### {name} — horizon\n" + confusion(G, P, 1, ["SHORT", "MEDIUM", "LONG"]) + "\n")
print("predicted horizon mix: ref", dict(Counter(v[1] for v in G.values())),
      *[f"| {n} {dict(Counter(v[1] for v in P.values()))}" for n, P in main.items()])

print("\n## By asset (recall / precision / direction / horizon)\n")
print("| asset | ref calls | " + " | ".join(main) + " |\n|---|---|" + "---|" * len(main))
for x in ASSETS:
    g = {k: v for k, v in G.items() if k[1] == x}
    cells = []
    for P in main.values():
        s = score(g, {k: v for k, v in P.items() if k[1] == x})
        cells.append(f"{s['rec']:.2f} / {s['prec']:.2f} / {s['dir']:.2f} / {s['hor']:.2f}")
    print(f"| {x} | {len(g)} | " + " | ".join(cells) + " |")

print("\n## By year (recall / precision / direction / horizon)\n")
year = {i: pending[i]["created_at"][:4] for i in ids}
years = sorted(set(year.values()))
print("| year | tweets | ref calls | " + " | ".join(main) + " |\n|---|---|---|" + "---|" * len(main))
for y in years:
    g = {k: v for k, v in G.items() if year[k[0]] == y}
    cells = []
    for P in main.values():
        s = score(g, {k: v for k, v in P.items() if year[k[0]] == y})
        cells.append(f"{s['rec']:.2f} / {s['prec']:.2f} / {s['dir']:.2f} / {s['hor']:.2f}")
    print(f"| {y} | {sum(1 for i in ids if year[i] == y)} | {len(g)} | " + " | ".join(cells) + " |")

print("\n## Pairwise agreement between models (F1 on calls / direction / horizon on shared)\n")
names = list(main)
print("| | " + " | ".join(names) + " |\n|---|" + "---|" * len(names))
for a in names:
    cells = []
    for b in names:
        if a == b:
            cells.append("–")
        else:
            s = score(main[a], main[b])
            cells.append(f"{s['f1']:.2f} / {s['dir']:.2f} / {s['hor']:.2f}")
    print(f"| {a} | " + " | ".join(cells) + " |")

print("\n## Misses shared across models (reference calls nobody found / only Opus found / etc.)\n")
missed_by = {name: {k for k in G if k not in P} for name, P in main.items()}
all_miss = set.intersection(*missed_by.values())
print(f"reference calls missed by all four: {len(all_miss)} of {len(G)}")
text = {i: pending[i]["text"] for i in ids}
for k in sorted(all_miss)[:12]:
    q = next((c["quote"] for c in fable[k[0]]["calls"] if c["asset"] == k[1]), "")
    print(f"  {k[1]:<4} {G[k][0]:<7} {G[k][1]:<6} quote={q[:70]!r} | {text[k[0]][:100]!r}")
print(f"\nmissed by every non-frontier model but found by Opus: "
      f"{len(set.intersection(*[missed_by[n] for n in names if n != 'Opus 5.5']) - missed_by['Opus 5.5'])}")

print("\n## Hybrid: Jev gate → Qwen3.6 labels\n")
print("| gate | passed | recall | precision | direction | horizon | e2e cell |\n|---|---|---|---|---|---|---|")
Q = preds["Qwen3.6-35B (prod)"]
s = score(G, Q)
print(f"| none | {len(ids)} (100%) | {s['rec']:.2f} | {s['prec']:.2f} | {s['dir']:.2f} | {s['hor']:.2f} | {s['e2e_cell']:.2f} |")
for thr in (0.2, 0.3):
    passing = {i for i in ids if jev_calls(jev[i], thr)}
    H = {k: v for k, v in Q.items() if k[0] in passing}
    s = score(G, H)
    print(f"| Jev p≥{thr} | {len(passing)} ({len(passing) / len(ids):.0%}) | {s['rec']:.2f} | {s['prec']:.2f} | "
          f"{s['dir']:.2f} | {s['hor']:.2f} | {s['e2e_cell']:.2f} |")
