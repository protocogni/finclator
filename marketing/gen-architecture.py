#!/usr/bin/env python3
"""Generates marketing/architecture.svg in the finclator brand world.
Tokens mirror public/site.css :root. Numbers come from public/site.json (matrix, hit rates, showcase call).
Run: python3 marketing/gen-architecture.py && bash marketing/render.sh"""
import json
import os

here = os.path.dirname(os.path.abspath(__file__))
site = json.load(open(os.path.join(here, "..", "public", "site.json")))

# --- tokens (public/site.css) ---
PAPER = "#f5f2ec"; PAPER2 = "#ede9e1"; CARD = "#ffffff"
INK = "#171512"; INK2 = "#4a463f"; INK3 = "#6b665d"
LINE = "#d9d4ca"; LINE2 = "#c6c0b4"
BUY = "#1f6f43"; BUY_BG = "#dcecdf"; SELL = "#a8352c"; SELL_BG = "#f1dcd8"; FLAT = "#5c5750"; FLAT_BG = "#e6e2da"
ACCENT = "#2f4c9c"; ACCENT_INK = "#22397a"; ACCENT_BG = "#e2e7f3"
ROLES = {
    "collect": dict(fg=INK3, tint=PAPER2, accent=LINE2),
    "retrieve": dict(fg=ACCENT_INK, tint=ACCENT_BG, accent=ACCENT),
    "extract": dict(fg=ACCENT_INK, tint=ACCENT_BG, accent=ACCENT),
    "grade": dict(fg=BUY, tint=BUY_BG, accent=BUY),
    "vote": dict(fg=INK2, tint=FLAT_BG, accent=INK),
}
FONT = 'Inter, -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif'
MONO = '"JetBrains Mono", ui-monospace, Menlo, monospace'
# type roles: title 34/700 · lede 15 · card-title 16/600 · card-sub 11.5/500 · body 12.5 · label 11/500
out = []


def text(x, y, s, fill=INK2, size=12.5, weight=400, anchor=None, extra=""):
    a = f' text-anchor="{anchor}"' if anchor else ""
    out.append(f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}"{a}{extra}>{s}</text>')


def label(x, y, s, fill=INK2, anchor="middle", extra=""):
    text(x, y, s, fill, 11, 500, anchor, f' paint-order="stroke" stroke="{PAPER}" stroke-width="4"{extra}')


def card(x, y, w, h, role, title, sub, lines, quote=None, lead=False, num=None):
    r = ROLES[role]
    stroke = r["accent"] if lead else LINE
    sw = 1.5 if lead else 1
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{CARD}" stroke="{stroke}" stroke-width="{sw}" filter="url(#sh)"/>')
    out.append(f'<path d="M{x+14} {y+1} H{x+w-14} a13 13 0 0 1 13 13 V{y+52} H{x+1} V{y+14} a13 13 0 0 1 13 -13z" fill="{r["tint"]}"/>')
    out.append(f'<path d="M{x+1} {y+52.5} H{x+w-1}" stroke="{LINE}"/>')
    if num is not None:
        out.append(f'<circle cx="{x+24}" cy="{y+26}" r="11" fill="{r["accent"]}"/>')
        text(x + 24, y + 30, str(num), CARD, 12, 700, "middle")
    text(x + 44, y + 24, title, INK, 16, 600, extra=' letter-spacing="-0.2"')
    text(x + 44, y + 42, sub, r["fg"], 11.5, 500)
    yy = y + 80
    for ln in lines:
        if ln is None:
            yy += 9
            continue
        bold = ln.startswith("**")
        indent = ln.startswith("  ")
        t = ln.strip("*").strip()
        text(x + 22 + (14 if indent else 0), yy, t, INK if bold else INK2, 12.5, 600 if bold else 400)
        yy += 19
    if quote:
        text(x + 22, y + h - 20, quote, INK2, 12, 400, extra=' font-style="italic"')


def arrow(d, color=LINE2, marker="a"):
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6" marker-end="url(#{marker})"/>')


# --- header ---
text(64, 76, "How Finclator keeps score on finance influencers", INK, 34, 700, extra=' letter-spacing="-0.9"')
text(64, 104, "Five layers: collect → retrieve → extract → grade → vote. Regex → Jev gate → Qwen3.6-35B, then price does the rest.", INK2, 15)
text(64, 124, "Every cell in the matrix traces back to a quote, a date, and two closing prices.", INK2, 15)

# --- row 1: the five layers (y 160–450) ---
R1, H1, W, GAP = 160, 290, 270, 30
xs = [64 + i * (W + GAP) for i in range(5)]

card(xs[0], R1, W, H1, "collect", "Collect", "twitterapi.io · originals only", [
    "about 100 accounts, by school",
    "replies / reposts rejected at",
    "  API call · response · DB insert",
    "per-account watermark:",
    "  fetched once, stored once",
    None,
    "**years of history per account**",
    "**(a 2-year call needs 2 years)**"], num=1)
card(xs[1], R1, W, H1, "retrieve", "Retrieve", "regex prefilter → TypeSafe Jev", [
    "pass 1: regex, names an asset at all",
    "  cheap, runs on everything",
    "pass 2: Jev decision model",
    "  “is this a falsifiable call?”",
    "  calibrated probability, no text",
    "  p ≥ 0.2 · 32 workers · ~3k/min",
    None,
    "**1 post in 5 reaches the LLM**"], num=2)
card(xs[2], R1, W, H1, "extract", "Extract", "Qwen3.6-35B-A3B q4 · Ollama · M5 Pro", [
    "4 posts per request · think off",
    "  ~115 posts/min, free at the margin",
    "asset · direction · horizon · target",
    "**quote = exact span of the post**",
    "  validated as a substring",
    None,
    "labels keyed (post, asset, model):",
    "  models coexist, never overwrite"], lead=True, num=3)
card(xs[3], R1, W, H1, "grade", "Grade", "Yahoo daily closes · mechanical", [
    "BTC-USD · GC=F · ^GSPC",
    "matures at 90 / 365 / 730 days",
    "flat band = ½ σ of the asset’s move",
    "CORRECT · PARTIAL · WRONG",
    "sane stated target hit / missed: ±0.25",
    None,
    "**trust = (hits + 5) / (n + 10)**",
    "**point-in-time: never sees the future**"], num=4)
card(xs[4], R1, W, H1, "vote", "Vote", "one vote per account per cell", [
    "its latest call in the window",
    "weight = trust × √confidence",
    "  × recency (half-life = window/3)",
    "|net| &lt; 0.15 → NEUTRAL",
    "fewer than 3 voters → N/A",
    None,
    "**3 assets × 3 horizons**",
    "**no single account can carry a cell**"], num=5)

for i, lbl in enumerate(("posts", "candidates", "calls", "outcomes")):
    x0 = xs[i] + W
    arrow(f"M{x0} {R1+150} H{x0+GAP}", INK, "aI")
    label(x0 + GAP / 2, R1 + 136, lbl)

# --- row 2 (y 500–800): audit example · matrix · baseline ---
R2, H2 = 500, 300

# audit card (one real call, from site.json)
ex = site["example"]
AX, AW = 64, 660
out.append(f'<rect x="{AX}" y="{R2}" width="{AW}" height="{H2}" rx="14" fill="{CARD}" stroke="{LINE}" filter="url(#sh)"/>')
text(AX + 24, R2 + 34, "What one graded call looks like", INK, 16, 600, extra=' letter-spacing="-0.2"')
text(AX + 24, R2 + 54, "every row on the audit page has these fields · the quote is highlighted inside the post", INK2, 12)
# pills
pills = [(f"@{ex['handle']}", PAPER2, INK, LINE2), (ex["asset"], FLAT_BG, FLAT, LINE2),
         (ex["direction"], BUY_BG if ex["direction"] == "BUY" else SELL_BG, BUY if ex["direction"] == "BUY" else SELL, LINE2),
         (ex["horizon"], ACCENT_BG, ACCENT_INK, LINE2), (f"target ${ex['price_target']:,.0f}", PAPER2, INK, LINE2)]
px = AX + 24
for lbl, bg, fg, br in pills:
    w = len(lbl) * 7.4 + 22
    out.append(f'<rect x="{px}" y="{R2+72}" width="{w}" height="26" rx="13" fill="{bg}" stroke="{br}"/>')
    text(px + w / 2, R2 + 89, lbl, fg, 12, 600, "middle")
    px += w + 8
q = ex["quote"]
if len(q) > 70:
    cut = q.rfind(" ", 0, 70)
    q1, q2 = q[:cut], q[cut + 1:]
else:
    q1, q2 = q, ""
text(AX + 24, R2 + 128, "“" + q1 + ("" if q2 else "”"), INK, 13, 400, extra=' font-style="italic"')
if q2:
    text(AX + 24, R2 + 147, q2 + "”", INK, 13, 400, extra=' font-style="italic"')
rows = [
    ("entry", f"{ex['entry_date']}  ·  close {ex['entry_close']:,.0f}"),
    ("exit", f"{ex['exit_date']}  ·  close {ex['exit_close']:,.0f}  ·  +90 days"),
    ("return", f"+{ex['return_pct']:.1f}%  vs flat band ±{ex['threshold_pct']:.1f}%"),
    ("target", f"${ex['price_target']:,.0f} touched inside the horizon → +0.25"),
]
yy = R2 + 182
for k, v in rows:
    text(AX + 24, yy, k, INK3, 11.5, 500)
    text(AX + 96, yy, v, INK2, 12.5, 400, extra=f' font-family=\'{MONO}\'')
    yy += 22
out.append(f'<rect x="{AX+24}" y="{R2+H2-46}" width="96" height="26" rx="13" fill="{BUY_BG}" stroke="{BUY}"/>')
text(AX + 72, R2 + H2 - 29, ex["result"], BUY, 12, 700, "middle")
text(AX + 134, R2 + H2 - 29, "→ one hit in this account’s BTC · SHORT trust cell", INK2, 12.5)

# matrix (the product)
MX, MW = 1154, 380
out.append(f'<rect x="{MX}" y="{R2}" width="{MW}" height="{H2}" rx="14" fill="{CARD}" stroke="{INK}" stroke-width="1.5" filter="url(#sh)"/>')
text(MX + 24, R2 + 34, "The product: a live 3×3", INK, 16, 600, extra=' letter-spacing="-0.2"')
text(MX + 24, R2 + 54, f"finclator.com · rebuilt daily · this one from {site['matrix_generated_at']}", INK2, 12)
assets = ("BTC", "GOLD", "SPX"); hors = ("SHORT", "MEDIUM", "LONG")
gx, gy, cw, ch = MX + 72, R2 + 96, 96, 50
for j, h in enumerate(hors):
    text(gx + j * cw + cw / 2, gy - 10, h, INK3, 11, 500, "middle")
for i, a in enumerate(assets):
    text(gx - 12, gy + i * ch + ch / 2 + 4, a, INK, 12.5, 600, "end")
    for j, h in enumerate(hors):
        c = site["matrix"][f"{a}:{h}"]
        bg, fg = {"BUY": (BUY_BG, BUY), "SELL": (SELL_BG, SELL), "NEUTRAL": (FLAT_BG, FLAT)}[c["label"]]
        out.append(f'<rect x="{gx+j*cw+3}" y="{gy+i*ch+3}" width="{cw-6}" height="{ch-6}" rx="8" fill="{bg}"/>')
        text(gx + j * cw + cw / 2, gy + i * ch + 24, c["label"], fg, 13, 700, "middle")
        text(gx + j * cw + cw / 2, gy + i * ch + 40, f"{c['accounts']} accts", fg, 10.5, 500, "middle")
text(MX + 24, R2 + H2 - 44, "0–3 mo · 3–12 mo · 1–5 y. Click a cell for its voters,", INK2, 12)
text(MX + 24, R2 + H2 - 24, "each with their quote. Plus trust grids, method, full audit.", INK2, 12)

# baseline card
BX, BW = 764, 360
out.append(f'<rect x="{BX}" y="{R2}" width="{BW}" height="{H2}" rx="14" fill="{CARD}" stroke="{LINE}" filter="url(#sh)"/>')
text(BX + 24, R2 + 34, "Labeler benchmark", INK, 16, 600, extra=' letter-spacing="-0.2"')
text(BX + 24, R2 + 54, "1,000 posts · reference = Claude Fable 5.1 · per (post, asset) call", INK2, 12)
# docs/JEV_EXPERIMENTS.md, "Per call" + "production hybrid" tables
bench = [
    ("Opus 5.5", "0.91", "0.82", "0.98", "0.89"),
    ("Jev p ≥ 0.2", "0.88", "0.63", "0.97", "0.83"),
    ("Qwen3.6-35B", "0.79", "0.66", "0.96", "0.81"),
    ("Jev → Qwen (prod)", "0.77", "0.79", "0.96", "0.82"),
]
tx = BX + 24
cols = (160, 212, 266, 312)
text(tx, R2 + 96, "model", INK3, 11, 500)
for cx, h in zip(cols, ("recall", "prec.", "dir.", "horizon")):
    text(tx + cx, R2 + 96, h, INK3, 11, 500, "end")
out.append(f'<path d="M{tx} {R2+104} H{BX+BW-24}" stroke="{LINE}"/>')
yy = R2 + 128
for name, *vals in bench:
    prod = name.endswith("(prod)")
    text(tx, yy, name, INK, 12.5, 600 if prod else 500)
    for cx, v in zip(cols, vals):
        text(tx + cx, yy, v, ACCENT_INK if prod else INK2, 13, 600 if prod else 400, "end", f' font-family=\'{MONO}\'')
    yy += 26
text(BX + 24, R2 + 244, "Two frontier models disagree on 1 call in 10.", INK, 12.5, 600)
text(BX + 24, R2 + 264, "The gate cuts GPU work 80% for 2 pts of recall;", INK2, 12.5)
text(BX + 24, R2 + 282, "precision rises because the false positives differ.", INK2, 12.5)

# row-1 → row-2 arrows (grade feeds the audit example; extract feeds the benchmark; vote feeds the matrix)
gx0 = xs[3] + W / 2
ex0 = xs[2] + W / 2
# blue: extract → benchmark, straight drop (ex0 lies inside the benchmark card's span)
arrow(f"M{ex0} {R1+H1} V{R2}", ACCENT, "aB"); label(ex0 + 12, R1 + H1 + 30, "benchmarked", ACCENT_INK, "start")
# green: grade → audit example; runs left along y+12 with a hop over the blue drop
yl = R1 + H1 + 12
arrow(f"M{gx0} {R1+H1} V{yl} H{ex0+8} a8 8 0 0 0 -16 0 H{AX+AW-40} V{R2}", BUY, "aG")
label(AX + AW - 28, R1 + H1 + 40, "graded call", BUY, "start")
arrow(f"M{xs[4]+W/2} {R1+H1} V{R2}", INK, "aI"); label(xs[4] + W / 2 + 12, R1 + H1 + 30, "matrix", INK, "start")

# --- legend + footer ---
lx = 64
for key, lbl in (("collect", "collection"), ("retrieve", "retrieval + LLM"), ("grade", "grading"), ("vote", "aggregation")):
    r = ROLES[key]
    out.append(f'<rect x="{lx}" y="{834}" width="14" height="14" rx="4" fill="{r["tint"]}" stroke="{r["accent"]}"/>')
    text(lx + 22, 846, lbl)
    lx += 22 + len(lbl) * 7.6 + 34
text(1534, 846, "finclator.com · BTC-USD, gold front-month, S&amp;P 500 · not investment advice", INK2, 12, 400, "end")


def mk(i, c):
    return f'<marker id="{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{c}"/></marker>'


body = "\n  ".join(out)
svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900" font-family='{FONT}'>
  <defs>
    <filter id="sh" x="-5%" y="-5%" width="110%" height="115%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="{INK}" flood-opacity="0.07"/></filter>
    {mk("a", LINE2)}{mk("aI", INK)}{mk("aG", BUY)}{mk("aB", ACCENT)}
  </defs>
  <rect width="1600" height="900" fill="{PAPER}"/>
  {body}
</svg>
"""
open(os.path.join(here, "architecture.svg"), "w").write(svg)
print("wrote architecture.svg")
