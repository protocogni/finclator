"""Holdout (English subset): Opus 5.5 vs Fable 5.1 (frontier-vs-frontier ceiling), and Jev / Qwen vs each of them.
Uses stored Jev answers (data/tune_jev_holdout.jsonl) and Qwen batch-4 preds (data/tune_C_prod_confirm.jsonl)."""
import json

ASSETS = ("BTC", "GOLD", "SPX")
D = {"up": "BUY", "down": "SELL", "neutral": "NEUTRAL"}
R = "data/"


def calls_of(rec, tid):
    if not rec.get("is_call"):
        return {}
    return {(tid, c["asset"]): (c["direction"], c["horizon"]) for c in rec.get("calls", []) if c.get("asset") in ASSETS}


def score(G, P):
    both = set(G) & set(P)
    n = len(both) or 1
    d = sum(G[k][0] == P[k][0] for k in both)
    h = sum(G[k][1] == P[k][1] for k in both)
    a = sum(G[k] == P[k] for k in both)
    rec, prec = len(both) / (len(G) or 1), len(both) / (len(P) or 1)
    f1 = 2 * rec * prec / (rec + prec) if rec + prec else 0
    return (f"ref={len(G):>2} pred={len(P):>2} both={len(both):>2} | recall {rec:.2f} precision {prec:.2f} F1 {f1:.2f} | "
            f"direction {d / n:.2f} horizon {h / n:.2f} cell {a / n:.2f}")


opus = {json.loads(ln)["id"]: json.loads(ln) for ln in open(R + "labels_holdout_en_opus55.jsonl")}
fable = {json.loads(ln)["id"]: json.loads(ln) for ln in open(R + "labels_holdout.jsonl")}
jev = {json.loads(ln)["id"]: json.loads(ln) for ln in open(R + "tune_jev_holdout.jsonl")}
qwen = {json.loads(ln)["id"]: json.loads(ln) for ln in open(R + "tune_C_prod_confirm.jsonl")}
ids = [i for i in opus if i in fable]
print(f"holdout English: {len(ids)} tweets; Jev answers for {sum(i in jev for i in ids)}, Qwen preds for {sum(i in qwen for i in ids)}")
OP, F = {}, {}
for i in ids:
    OP.update(calls_of(opus[i], i))
    F.update(calls_of(fable[i], i))
print("tweet-level is_call agreement Opus vs Fable:", f"{sum(bool(opus[i]['is_call']) == bool(fable[i]['is_call']) for i in ids) / len(ids):.3f}")
print("Opus 5.5 vs Fable 5.1 (ref=Fable):", score(F, OP))

ids_j = [i for i in ids if i in jev]
J = {}
for i in ids_j:
    a = jev[i]["raw"]["answers"]
    if a["is_call"]["noul"] >= 0.3:
        for x in ASSETS:
            st = a.get(f"stance_{x}")
            if st and st["choice"] != "none":
                J[(i, x)] = (D[st["choice"]], a.get(f"horizon_{x}", {}).get("choice"))
Q = {}
ids_q = [i for i in ids if i in qwen]
for i in ids_q:
    Q.update(calls_of(qwen[i]["pred"], i))
Fj = {k: v for k, v in F.items() if k[0] in set(ids_j)}
Oj = {k: v for k, v in OP.items() if k[0] in set(ids_j)}
Fq = {k: v for k, v in F.items() if k[0] in set(ids_q)}
Oq = {k: v for k, v in OP.items() if k[0] in set(ids_q)}
print(f"Jev@0.3 vs Fable  (n={len(ids_j)}):", score(Fj, J))
print(f"Jev@0.3 vs Opus   (n={len(ids_j)}):", score(Oj, J))
print(f"Qwen    vs Fable  (n={len(ids_q)}):", score(Fq, Q))
print(f"Qwen    vs Opus   (n={len(ids_q)}):", score(Oq, Q))
print("\nOpus/Fable disagreements on the holdout (is_call):")
for i in ids:
    if bool(opus[i]["is_call"]) != bool(fable[i]["is_call"]):
        print(f"  fable={fable[i]['is_call']!s:<5} opus={opus[i]['is_call']!s:<5} | {jev.get(i, {}).get('text', '')[:110]!r}")
both = set(OP) & set(F)
print("Opus/Fable disagreements on direction/horizon among shared calls:", [(k[1], F[k], OP[k]) for k in both if F[k] != OP[k]])
