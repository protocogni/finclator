# Jev as a call classifier — experiment notes

Working notes for the AI Tinkerers talk. Every number below comes from a script in `scripts/` and a file in
`data/`; nothing is estimated. English tweets only.

## The question

Finclator turns finance-influencer tweets into explicit, falsifiable market calls — per tweet and asset:
`direction ∈ {BUY, SELL, NEUTRAL}`, `horizon ∈ {SHORT 0–3 mo, MEDIUM 3–12 mo, LONG 1–5 y}`, plus a verbatim
`quote` and an optional `price_target`. Calls are graded against price when the horizon matures and rolled into a
3×3 BUY/NEUTRAL/SELL matrix (BTC, gold, S&P 500 × three horizons).

Jev (TypeSafe's decision model) answers **typed questions with calibrated probabilities and no text**. In production
it is only the `is_call` gate in front of the local text model (`src/gate.py`). The experiment here asks the bigger
question: **can Jev produce the matrix inputs on its own — direction and horizon — and how does it compare to the
production classifier and to a frontier model on the same tweets?**

## Setup

| Piece | What |
|---|---|
| Tweet set | 400 English original tweets that mention BTC, gold or the S&P 500, sampled at random from the corpus (68 accounts, ≤ 8 per account, 2023–2026), none in any earlier gold set. `scripts/sample_tweets.py 400 8` → `data/pending_en.jsonl`. |
| Reference labels | **Claude Opus 5.5** (reasoning: high), carrying the verbatim classifier SYSTEM prompt from `src/classify.py`, 40 tweets per request, quotes checked as substrings of the tweet. `scripts/label_frontier.py` → `data/labels_en_opus55.jsonl`. 400/400 answered, 75 call tweets, 80 (tweet, asset) calls, 0 malformed, 0 bad quotes, 113 s wall. |
| Jev | `jev-latest` (answers as `jev-1.13.0`), direct API `api.typesafe.ai/v1/systemone`, 32 workers. The SYSTEM prompt decomposed into typed questions: `is_call` (**noul** → one probability), and per asset mentioned a `stance` **choice** (none / up / down / neutral) and a `horizon` **choice** (SHORT / MEDIUM / LONG). Question text: `scripts/score_jev.py`. `scripts/jev_answers.py` → `data/jev_raw_en.jsonl`. 400/400, 0 errors, **3.5 s wall, 6,848 tweets/min, 0.24 s median latency, 575k input tokens ≈ $0.024**. |
| Qwen (production) | Qwen3.6-35B-A3B q4_K_M on Ollama, Apple M5 Pro 64 GB; production config (4 tweets per request, terse output, thinking off, 8 workers, same SYSTEM prompt). `scripts/qwen_answers.py` → `data/qwen_preds_en.jsonl`. 400/400, 314 s, **77 tweets/min**. |
| Scoring | `scripts/compare_labelers.py en`. Unit = one (tweet, asset) call, i.e. what the matrix consumes. *recall* = reference calls the model also called; *precision* = model calls that are in the reference; *direction / horizon / cell* = agreement on the shared calls (cell = both right); *e2e cell* = reference calls that came out fully right (recall × cell). |

A call to Jev is "a call" when `p_call ≥ threshold` **and** the stance for that asset ≠ `none`. Direction is the
stance choice, horizon is the horizon choice.

## Results — reference: Opus 5.5, 400 English tweets, 80 reference calls

### Per-call quality

| Model | recall | precision | F1 | direction | horizon | cell (dir+hor) | e2e cell |
|---|---|---|---|---|---|---|---|
| Qwen3.6 batch-4 (production) | 0.74 | 0.69 | 0.71 | **0.97** | **0.85** | **0.81** | 0.60 |
| Jev p ≥ 0.2 | **0.85** | 0.72 | **0.78** | 0.94 | 0.81 | 0.75 | **0.64** |
| Jev p ≥ 0.3 (production gate threshold) | 0.75 | 0.75 | 0.75 | 0.93 | 0.80 | 0.73 | 0.55 |
| Jev p ≥ 0.4 | 0.66 | 0.79 | 0.72 | 0.94 | 0.79 | 0.74 | 0.49 |
| Jev p ≥ 0.5 | 0.57 | 0.84 | 0.68 | 0.93 | 0.78 | 0.72 | 0.41 |
| Jev p ≥ 0.6 | 0.51 | 0.85 | 0.64 | 0.93 | 0.76 | 0.68 | 0.35 |

### Tweet-level `is_call` (reference call rate: 19 % of tweets)

| Model | accuracy | precision | recall |
|---|---|---|---|
| Qwen3.6 batch-4 | 0.892 | 0.70 | 0.75 |
| Jev p ≥ 0.2 | **0.912** | 0.72 | **0.87** |
| Jev p ≥ 0.3 | 0.910 | 0.76 | 0.76 |
| Jev p ≥ 0.4 | 0.907 | 0.81 | 0.67 |
| Jev p ≥ 0.5 | 0.900 | 0.84 | 0.57 |

### Confusion on shared calls (rows = Opus, columns = model)

Jev @0.3 — direction (60 calls) | Jev @0.3 — horizon
--- | ---
BUY → 41 BUY, 2 NEUTRAL, 0 SELL | SHORT → 25 SHORT, 0, 0
NEUTRAL → 1 BUY, 4 NEUTRAL, 1 SELL | MEDIUM → 6 SHORT, 11 MEDIUM, 4 LONG
SELL → 0, 0, 11 SELL | LONG → 1 SHORT, 1 MEDIUM, 12 LONG

Qwen — direction (59 calls) | Qwen — horizon
--- | ---
BUY → 45 BUY, 0, 0 | SHORT → 23 SHORT, 1 MEDIUM, 0
NEUTRAL → 0, 1 NEUTRAL, 1 SELL | MEDIUM → 4 SHORT, 12 MEDIUM, 2 LONG
SELL → 0, 1 NEUTRAL, 11 SELL | LONG → 1 SHORT, 1 MEDIUM, 15 LONG

Neither model ever flips BUY ↔ SELL. Every direction miss involves NEUTRAL (two-sided levels, "local top forming…
eyes on 74k", a support level "which should hold"). Every horizon miss is MEDIUM leaking into its neighbours —
Jev leans SHORT (predicted mix 44/15/21 vs reference 32/26/22), Qwen is closer (39/24/23).

### By asset (Jev @0.3 / Qwen)

| Asset | ref calls | recall | precision | direction | horizon |
|---|---|---|---|---|---|
| BTC | 48 | 0.83 / 0.77 | 0.78 / 0.74 | 0.93 / 0.95 | 0.75 / 0.81 |
| GOLD | 15 | 0.80 / 0.87 | 0.67 / 0.57 | 1.00 / 1.00 | 0.92 / 0.85 |
| SPX | 17 | 0.47 / 0.53 | 0.73 / 0.69 | 0.88 / 1.00 | 0.88 / 1.00 |

S&P 500 calls are the hard ones for both: the reference counts positioning statements ("added a small handful of
longs", "splitting my remaining long into two tranches") and index-move commentary as calls; both models read
those as observations.

### Production hybrid: Jev gate → Qwen labels

| Gate | tweets passed to Qwen | recall | precision | direction | horizon | e2e cell |
|---|---|---|---|---|---|---|
| no gate (Qwen alone) | 400 (100 %) | 0.74 | 0.69 | 0.97 | 0.85 | 0.60 |
| Jev p ≥ 0.2 | 90 (22 %) | 0.70 | 0.78 | 0.96 | 0.84 | 0.56 |
| Jev p ≥ 0.3 (current) | 75 (19 %) | 0.65 | 0.79 | 0.96 | 0.83 | 0.51 |
| stance ≠ none only | 108 (27 %) | 0.71 | 0.71 | 0.96 | 0.84 | 0.57 |

The gate at 0.3 costs 9 points of recall against Qwen alone for an 81 % cut in GPU work. At 0.2 it costs 4 points
for a 78 % cut. Precision goes *up* under the gate — Jev and Qwen make different false positives, and the
conjunction removes some.

### What Jev misses (p ≥ 0.3, 20 of 80 reference calls)

`p_call` of the misses: 14 sit at 0.1–0.2, 6 above. The 0.1–0.2 band is the interesting one — Jev already assigns
the right stance to most of them (`stance=up/down`) but the noul says "probably not a call":

- "There's a 65% chance of a US strategic reserve for Bitcoin and you can still buy it for under $70K" — p 0.22, stance up
- "Price still in the LGC buy-zone for investors" — p 0.22, stance up
- "This is EXACTLY why I have been warning you guys that stage 3 was NOT a place to long" — p 0.22, stance down
- "Stage 4 downtrend, and indicators all red. Nothing here excites me" — p 0.22, stance down

Implicit calls: the stance is in the framing, the tweet never says "will go up". The instructions carry "be strict,
when in doubt answer no", and that phrase is what pushes these to 0.2. The same tweets are among Qwen's 21 misses.

### What Jev adds (20 extras at p ≥ 0.3)

Chart-caption tweets ("#Bitcoin $40k🚀 + image", "The Daily Downtrend is over + image"), macro narratives without a
price claim (national-debt tweets read as GOLD BUY LONG), and one tweet about another asset carrying a #Bitcoin
tag ($GME → BTC BUY). The SYSTEM prompt's NOT-a-call list names all three; in the typed decomposition they are
strings inside `instructions`, and the noul does not weigh them as hard as a text model reading the same prompt.

## Frontier-vs-frontier ceiling (the earlier holdout)

The first gold set was labeled by Claude Fable 5.1 interactively. Re-labeling its 105 English tweets with Opus 5.5
(`scripts/frontier_ceiling.py`) gives the agreement two frontier models reach on this task:

| Comparison | n | recall | precision | F1 | direction | horizon | cell |
|---|---|---|---|---|---|---|---|
| **Opus 5.5 vs Fable 5.1** | 105 | 1.00 | 0.93 | 0.96 | 0.92 | 0.92 | 0.85 |
| Jev @0.3 vs Fable | 87 | 1.00 | 0.78 | 0.88 | 1.00 | 0.57 | 0.57 |
| Jev @0.3 vs Opus | 87 | 0.88 | 0.78 | 0.82 | 1.00 | 0.43 | 0.43 |
| Qwen vs Fable | 105 | 0.85 | 0.92 | 0.88 | 1.00 | 0.73 | 0.73 |
| Qwen vs Opus | 105 | 0.79 | 0.92 | 0.85 | 1.00 | 0.82 | 0.82 |

Tweet-level `is_call` agreement between the two frontier models: 0.99 (one disagreement: a halving-cycle progress
bar). On direction and horizon they disagree on 2 of 13 shared calls (NEUTRAL vs BUY on a two-sided level; LONG vs
MEDIUM on an undated cycle claim). So ~0.92 direction / ~0.92 horizon is the ceiling; nobody should be graded
against 1.00. (This holdout has only 7–14 calls; the 400-tweet set above is the one to quote.)

## What this says

1. **On `is_call`, Jev is the best model in the stack.** At p ≥ 0.2 it finds 87 % of the reference call tweets
   with accuracy 0.912, vs 0.75 / 0.892 for the production 35B — and it does that in 0.24 s per tweet for six
   thousandths of a cent, 90× faster than the local GPU path.

2. **On direction, Jev is at the frontier ceiling.** 0.93–0.94 agreement with Opus on the 400-set, 1.00 with
   both frontier models on the holdout, zero BUY ↔ SELL flips anywhere. The stance question is a solved problem
   for a typed decision model.

3. **On horizon, Jev is close but leans SHORT.** 0.80 vs Qwen's 0.85 vs a 0.92 frontier ceiling. The MEDIUM class
   ("undated expectation, no level game, no structural argument") is a residual category, and a choice question has
   no way to express "neither of the other two". Two nouls — "is this a level/pattern trade?" and "is this a
   structural/multi-year thesis?" — with MEDIUM = neither is the next thing to try.

4. **The threshold belongs to the prompt.** "Be strict, when in doubt answer no" in the noul instructions pushes
   implicit calls to p ≈ 0.2. On this set 0.2 beats 0.3 on every end-to-end number; the production gate should
   move to 0.2 (recall 0.70 → hybrid, from 0.65, for 15 more tweets per 400 on the GPU).

5. **Jev cannot replace the text model for this product** — not because of accuracy but because of output shape:
   there is no `quote` (the audit page's evidence column) and no `price_target` (± credit in scoring). Jev decides,
   the text model extracts. That division of labour is the design.

6. **Jev and Qwen disagree with each other about as much as either disagrees with Opus** (F1 0.78, direction 0.92,
   horizon 0.82 between them). A `dir-disagree` audit flag where Jev's stance ≠ Qwen's direction is free and would
   surface the two-sided / support-level cases that trip both.

## Reproduce

```
PYTHONPATH=. .venv/bin/python scripts/sample_tweets.py 400 8                     # data/pending_en.jsonl
PYTHONPATH=. .venv/bin/python scripts/label_frontier.py data/pending_en.jsonl data/labels_en_opus55.jsonl 40 4
PYTHONPATH=. .venv/bin/python scripts/jev_answers.py data/pending_en.jsonl en    # data/jev_raw_en.jsonl
PYTHONPATH=. .venv/bin/python scripts/qwen_answers.py data/pending_en.jsonl en   # data/qwen_preds_en.jsonl (Ollama up)
PYTHONPATH=. .venv/bin/python scripts/compare_labelers.py en
PYTHONPATH=. .venv/bin/python scripts/frontier_ceiling.py
```

`label_frontier.py` runs the frontier model through `hermes chat -m claude-opus-5-5 --reasoning high` with the
SYSTEM prompt in the query, so the reference labels come from the same rules as every other labeler. `jev_answers.py`
reads `TYPESAFE_API_KEY` from `.env`.

## Open

- Two-noul horizon shape for Jev (see 3).
- Gate threshold 0.3 → 0.2 in `src/gate.py` (see 4), then re-measure the daily gate pass rate.
- A Jev-only matrix from the full corpus (~10 min, ~$3) next to the production matrix, as a talk artifact — with the
  audit page tolerating NULL quotes.
- The 400-set reference is one frontier model's opinion; Fable-vs-Opus shows ~8 % of direction/horizon labels are
  judgment calls. A second frontier pass on the 400 would give the ceiling on the same set.
