"""Three-way comparison on one tweet set: Opus 5.5 labels (reference) vs Jev (typed questions) vs Qwen (production).
Per (tweet, asset) call: recall / precision / direction / horizon / exact-cell; confusion matrices; Jev threshold sweep;
Jev vs Qwen agreement with each other. Also the 100-tweet holdout (Fable reference) for Jev+Qwen only.
Usage: PYTHONPATH=. .venv/bin/python scripts/compare_labelers.py <tag>   (reads data/labels_en_opus55.jsonl, jev_raw_<tag>, qwen_preds_<tag>)"""
import json
import sys
from collections import Counter

ASSETS = ("BTC", "GOLD", "SPX")
D = {"up": "BUY", "down": "SELL", "neutral": "NEUTRAL"}
tag = sys.argv[1] if len(sys.argv) > 1 else "en"
ROOT = "data/"


def calls_of(rec, tid=None):
    tid = tid or rec["id"]
    if not rec.get("is_call"):
        return {}
    return {(tid, c["asset"]): (c["direction"], c["horizon"]) for c in rec.get("calls", [])
            if c.get("asset") in ASSETS and c.get("direction") and c.get("horizon")}


def jev_calls(r, thr):
    a = r["raw"]["answers"]
    out = {}
    if a["is_call"]["noul"] < thr:
        return out
    for x in ASSETS:
        st = a.get(f"stance_{x}")
        if st and st["choice"] != "none":
            out[(r["id"], x)] = (D[st["choice"]], a.get(f"horizon_{x}", {}).get("choice"))
    return out


def score(G, P):
    both = set(G) & set(P)
    d = sum(G[k][0] == P[k][0] for k in both)
    h = sum(G[k][1] == P[k][1] for k in both)
    a = sum(G[k] == P[k] for k in both)
    n = len(both) or 1
    return dict(gold=len(G), pred=len(P), both=len(both), rec=len(both) / (len(G) or 1), prec=len(both) / (len(P) or 1),
                dir=d / n, hor=h / n, cell=a / n, e2e_dir=d / (len(G) or 1), e2e_cell=a / (len(G) or 1))


def fmt(name, s):
    f1 = 2 * s["rec"] * s["prec"] / (s["rec"] + s["prec"]) if s["rec"] + s["prec"] else 0
    return (f"{name:<22} gold={s['gold']:>3} pred={s['pred']:>3} both={s['both']:>3} | recall {s['rec']:.2f} precision "
            f"{s['prec']:.2f} F1 {f1:.2f} | direction {s['dir']:.2f} horizon {s['hor']:.2f} cell {s['cell']:.2f} | "
            f"e2e direction {s['e2e_dir']:.2f} e2e cell {s['e2e_cell']:.2f}")


def confusion(G, P, idx, labels):
    both = set(G) & set(P)
    c = Counter((G[k][idx], P[k][idx]) for k in both)
    lines = ["ref \\ pred".ljust(12) + "".join(x.rjust(9) for x in labels)]
    for g in labels:
        lines.append(g.ljust(12) + "".join(str(c[(g, p)]).rjust(9) for p in labels))
    return "\n".join(lines)


def tweet_level(G_recs, P_is_call):
    n = len(G_recs)
    acc = sum(bool(G_recs[i]) == bool(P_is_call.get(i)) for i in G_recs) / n
    tp = sum(G_recs[i] and P_is_call.get(i) for i in G_recs)
    fp = sum((not G_recs[i]) and P_is_call.get(i) for i in G_recs)
    fn = sum(G_recs[i] and not P_is_call.get(i) for i in G_recs)
    pr = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    return acc, pr, rc


opus = {json.loads(ln)["id"]: json.loads(ln) for ln in open(ROOT + "labels_en_opus55.jsonl")}
jev = {json.loads(ln)["id"]: json.loads(ln) for ln in open(ROOT + f"jev_raw_{tag}.jsonl")}
qwen = {json.loads(ln)["id"]: json.loads(ln) for ln in open(ROOT + f"qwen_preds_{tag}.jsonl")}
ids = [i for i in opus if i in jev and i in qwen]
print(f"== English set: {len(ids)} tweets | Opus 5.5 reference: is_call tweets {sum(opus[i]['is_call'] for i in ids)}, "
      f"calls {sum(len(opus[i]['calls']) for i in ids)}")
G = {}
for i in ids:
    G.update(calls_of(opus[i]))
print("   reference direction:", dict(Counter(v[0] for v in G.values())), "horizon:", dict(Counter(v[1] for v in G.values())),
      "asset:", dict(Counter(k[1] for k in G)))

Q = {}
for i in ids:
    Q.update(calls_of(qwen[i]["pred"], i))
print("\n-- per (tweet, asset) call, reference = Opus 5.5 --")
print(fmt("Qwen3.6 batch-4", score(G, Q)))
for thr in (0.2, 0.3, 0.4, 0.5, 0.6):
    J = {}
    for i in ids:
        J.update(jev_calls(jev[i], thr))
    print(fmt(f"Jev p_call>={thr}", score(G, J)))

print("\n-- tweet-level is_call (accuracy / precision / recall), reference = Opus 5.5 --")
g_is = {i: bool(opus[i]["is_call"]) for i in ids}
acc, pr, rc = tweet_level(g_is, {i: bool(qwen[i]["pred"].get("is_call")) for i in ids})
print(f"{'Qwen3.6 batch-4':<22} acc {acc:.3f} prec {pr:.2f} rec {rc:.2f}")
for thr in (0.2, 0.3, 0.4, 0.5, 0.6):
    p_is = {i: bool(jev_calls(jev[i], thr)) for i in ids}
    acc, pr, rc = tweet_level(g_is, p_is)
    print(f"{'Jev p_call>=' + str(thr):<22} acc {acc:.3f} prec {pr:.2f} rec {rc:.2f}")

J3 = {}
for i in ids:
    J3.update(jev_calls(jev[i], 0.3))
print("\n-- confusion vs Opus, Jev @0.3 --")
print("direction:\n" + confusion(G, J3, 0, ["BUY", "NEUTRAL", "SELL"]))
print("horizon:\n" + confusion(G, J3, 1, ["SHORT", "MEDIUM", "LONG"]))
print("\n-- confusion vs Opus, Qwen --")
print("direction:\n" + confusion(G, Q, 0, ["BUY", "NEUTRAL", "SELL"]))
print("horizon:\n" + confusion(G, Q, 1, ["SHORT", "MEDIUM", "LONG"]))
print("\npredicted horizon mix: Opus", dict(Counter(v[1] for v in G.values())), "| Jev@0.3", dict(Counter(v[1] for v in J3.values())),
      "| Qwen", dict(Counter(v[1] for v in Q.values())))

print("\n-- by asset (vs Opus) --")
for x in ASSETS:
    g = {k: v for k, v in G.items() if k[1] == x}
    print(f"  {x}: " + fmt("Jev@0.3", score(g, {k: v for k, v in J3.items() if k[1] == x})))
    print(f"  {x}: " + fmt("Qwen", score(g, {k: v for k, v in Q.items() if k[1] == x})))

print("\n-- Jev vs Qwen agreement with each other (no reference) --")
print(fmt("Jev@0.3 vs Qwen", score(Q, J3)))

# combined: Jev gate then Qwen labels (production hybrid)
H = {k: v for k, v in Q.items() if k[0] in {i for i in ids if jev_calls(jev[i], 0.3)}}
print("\n-- production hybrid: Jev@0.3 gate → Qwen labels (vs Opus) --")
print(fmt("gate+Qwen", score(G, H)))
gate_pass = sum(bool(jev_calls(jev[i], 0.3)) for i in ids)
print(f"   gate passes {gate_pass}/{len(ids)} tweets ({gate_pass / len(ids):.0%}) to the text model")

# misses / extras samples
print("\n-- Jev@0.3 missed reference calls (p_call | stance | text) --")
text = {i: opus_text for i, opus_text in ((i, jev[i]["text"]) for i in ids)}
missed = [k for k in G if k not in J3]
for k in missed[:14]:
    a = jev[k[0]]["raw"]["answers"]
    st = a.get(f"stance_{k[1]}", {})
    print(f"  {k[1]:<4} {G[k][0]:<7} {G[k][1]:<6} p={a['is_call']['noul']:.2f} stance={st.get('choice')} | "
          f"{text[k[0]][:120]!r}")
print(f"  … {len(missed)} missed; p_call histogram {sorted(Counter(round(jev[k[0]]['raw']['answers']['is_call']['noul'], 1) for k in missed).items())}")
print("\n-- Jev@0.3 extra calls (not in reference) --")
extra = [k for k in J3 if k not in G]
for k in extra[:12]:
    a = jev[k[0]]["raw"]["answers"]
    print(f"  {k[1]:<4} {J3[k][0]:<7} {J3[k][1]:<6} p={a['is_call']['noul']:.2f} | {text[k[0]][:120]!r}")
print(f"  … {len(extra)} extra")
print("\n-- Qwen missed reference calls --")
qm = [k for k in G if k not in Q]
for k in qm[:8]:
    print(f"  {k[1]:<4} {G[k][0]:<7} {G[k][1]:<6} | {text[k[0]][:120]!r}")
print(f"  … {len(qm)} missed")
print("\n-- direction disagreements Jev@0.3 vs Opus --")
for k in [k for k in set(G) & set(J3) if G[k][0] != J3[k][0]][:10]:
    print(f"  {k[1]:<4} opus={G[k][0]:<7} jev={J3[k][0]:<7} | {text[k[0]][:120]!r}")
