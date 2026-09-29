# Jev as a call classifier: what I measured

Notes for the AI Tinkerers talk. Every number here comes from a script in `scripts/` and a file in `data/`,
nothing is estimated, and I only used English tweets.

## Why I ran this

Finclator reads finance-influencer tweets and pulls out explicit, falsifiable market calls. For each tweet and asset
it wants a direction (BUY, SELL or NEUTRAL), a horizon (SHORT 0-3 months, MEDIUM 3-12 months, LONG 1-5 years), the
exact quote that justifies the label, and a price target if the author named one. Calls get graded against price once
the horizon matures, and the graded record of every account feeds a BUY/NEUTRAL/SELL matrix for Bitcoin, gold and
the S&P 500 at the three horizons.

Jev, TypeSafe's decision model, answers typed questions with calibrated probabilities. No text comes back. In
production I only use it as the "is this a call at all?" gate in front of my local text model (`src/gate.py`). The
obvious follow-up question, which I had been avoiding because I assumed the answer was no: can Jev produce the two
things the matrix needs, direction and horizon, on its own? And how does it stack up against the model I actually run
and against a frontier model, on the same tweets?

## Setup

I wanted three labelers on one set of tweets, all reading the same rules.

The tweets: 400 English originals that mention BTC, gold or the S&P 500, drawn at random from the corpus. 68
accounts, at most 8 per account, 2023 through 2026, none of them in any earlier gold set.
`scripts/sample_tweets.py 400 8` writes `data/pending_en.jsonl`.

The reference: Claude Opus 5.5 with reasoning set to high, given the verbatim classifier SYSTEM prompt from
`src/classify.py`, 40 tweets per request, every quote checked as a substring of its tweet.
`scripts/label_frontier.py` writes `data/labels_en_opus55.jsonl`. It answered all 400: 75 call tweets, 80 (tweet,
asset) calls, zero malformed rows, zero bad quotes, 113 seconds.

Jev: `jev-latest`, which currently answers as `jev-1.13.0`, over the direct API at `api.typesafe.ai/v1/systemone`
with 32 workers. The SYSTEM prompt is decomposed into typed questions. `is_call` is a noul, so one probability comes
back. For each asset the tweet mentions there is a stance choice (none / up / down / neutral) and a horizon choice
(SHORT / MEDIUM / LONG). The question text lives in `scripts/score_jev.py`; `scripts/jev_answers.py` writes
`data/jev_raw_en.jsonl`. All 400 answered, zero errors, 3.5 seconds of wall time, 6,848 tweets a minute, 0.24 s
median latency, 575k input tokens, about $0.024.

Qwen, the production classifier: Qwen3.6-35B-A3B at q4_K_M on Ollama, on an M5 Pro with 64 GB. Production config
throughout, meaning 4 tweets per request, terse output, thinking off, 8 workers, same SYSTEM prompt.
`scripts/qwen_answers.py` writes `data/qwen_preds_en.jsonl`. All 400 in 314 seconds, 77 tweets a minute.

Scoring is `scripts/compare_labelers.py en`. The unit is one (tweet, asset) call, because that is what the matrix
consumes. Recall is the share of reference calls the model also called. Precision is the share of the model's calls
that exist in the reference. Direction, horizon and cell are agreement on the calls both sides made (cell means both
right). "e2e cell" is the share of reference calls that came out fully right, so recall times cell.

Jev counts as having made a call when `p_call` clears the threshold and the stance for that asset is not `none`.
Direction is the stance choice, horizon is the horizon choice.

## Results, reference = Opus 5.5, 400 tweets, 80 reference calls

### Per call

| Model | recall | precision | F1 | direction | horizon | cell (dir+hor) | e2e cell |
|---|---|---|---|---|---|---|---|
| Qwen3.6 batch-4 (production) | 0.74 | 0.69 | 0.71 | 0.97 | 0.85 | 0.81 | 0.60 |
| Jev p ≥ 0.2 | 0.85 | 0.72 | 0.78 | 0.94 | 0.81 | 0.75 | 0.64 |
| Jev p ≥ 0.3 (the production gate threshold) | 0.75 | 0.75 | 0.75 | 0.93 | 0.80 | 0.73 | 0.55 |
| Jev p ≥ 0.4 | 0.66 | 0.79 | 0.72 | 0.94 | 0.79 | 0.74 | 0.49 |
| Jev p ≥ 0.5 | 0.57 | 0.84 | 0.68 | 0.93 | 0.78 | 0.72 | 0.41 |
| Jev p ≥ 0.6 | 0.51 | 0.85 | 0.64 | 0.93 | 0.76 | 0.68 | 0.35 |

### Per tweet, is_call only (19 % of the tweets are calls in the reference)

| Model | accuracy | precision | recall |
|---|---|---|---|
| Qwen3.6 batch-4 | 0.892 | 0.70 | 0.75 |
| Jev p ≥ 0.2 | 0.912 | 0.72 | 0.87 |
| Jev p ≥ 0.3 | 0.910 | 0.76 | 0.76 |
| Jev p ≥ 0.4 | 0.907 | 0.81 | 0.67 |
| Jev p ≥ 0.5 | 0.900 | 0.84 | 0.57 |

### Where the disagreements are (rows = Opus, columns = the model)

Jev @0.3, direction, 60 shared calls | Jev @0.3, horizon
--- | ---
BUY → 41 BUY, 2 NEUTRAL, 0 SELL | SHORT → 25 SHORT, 0, 0
NEUTRAL → 1 BUY, 4 NEUTRAL, 1 SELL | MEDIUM → 6 SHORT, 11 MEDIUM, 4 LONG
SELL → 0, 0, 11 SELL | LONG → 1 SHORT, 1 MEDIUM, 12 LONG

Qwen, direction, 59 shared calls | Qwen, horizon
--- | ---
BUY → 45 BUY, 0, 0 | SHORT → 23 SHORT, 1 MEDIUM, 0
NEUTRAL → 0, 1 NEUTRAL, 1 SELL | MEDIUM → 4 SHORT, 12 MEDIUM, 2 LONG
SELL → 0, 1 NEUTRAL, 11 SELL | LONG → 1 SHORT, 1 MEDIUM, 15 LONG

Neither model flipped BUY and SELL even once. Every direction miss involves NEUTRAL: two-sided level conditionals, "local top forming, eyes on 74k", a support level "which should hold". Every
horizon miss is MEDIUM bleeding into a neighbour. Jev leans SHORT (its predicted mix is 44/15/21 against the
reference's 32/26/22); Qwen is closer at 39/24/23.

### By asset (Jev @0.3 / Qwen)

| Asset | ref calls | recall | precision | direction | horizon |
|---|---|---|---|---|---|
| BTC | 48 | 0.83 / 0.77 | 0.78 / 0.74 | 0.93 / 0.95 | 0.75 / 0.81 |
| GOLD | 15 | 0.80 / 0.87 | 0.67 / 0.57 | 1.00 / 1.00 | 0.92 / 0.85 |
| SPX | 17 | 0.47 / 0.53 | 0.73 / 0.69 | 0.88 / 1.00 | 0.88 / 1.00 |

S&P 500 calls are hard for both. The reference counts positioning statements ("added a small handful of longs",
"splitting my remaining long into two tranches") and index-move commentary as calls; both models read those as
observations. I am honestly not sure the reference is right on all of them.

### The production hybrid: Jev gate, then Qwen labels

| Gate | tweets passed to Qwen | recall | precision | direction | horizon | e2e cell |
|---|---|---|---|---|---|---|
| no gate (Qwen alone) | 400 (100 %) | 0.74 | 0.69 | 0.97 | 0.85 | 0.60 |
| Jev p ≥ 0.2 | 90 (22 %) | 0.70 | 0.78 | 0.96 | 0.84 | 0.56 |
| Jev p ≥ 0.3 (current) | 75 (19 %) | 0.65 | 0.79 | 0.96 | 0.83 | 0.51 |
| stance ≠ none only | 108 (27 %) | 0.71 | 0.71 | 0.96 | 0.84 | 0.57 |

The gate at 0.3 costs 9 points of recall against ungated Qwen and cuts GPU work by 81 %. At 0.2 it costs 4 points
and cuts 78 %. Precision actually goes up under the gate, because Jev and Qwen make different false positives and
the conjunction removes some of each.

### What Jev misses (p ≥ 0.3, 20 of 80 reference calls)

14 of the 20 misses sit at p_call 0.1 to 0.2, only 6 above. That band is the interesting one. Jev has already
assigned the right stance to most of them (`stance=up` or `down`), the noul just says "probably not a call":

- "There's a 65% chance of a US strategic reserve for Bitcoin and you can still buy it for under $70K" (p 0.22, stance up)
- "Price still in the LGC buy-zone for investors" (p 0.22, stance up)
- "This is EXACTLY why I have been warning you guys that stage 3 was NOT a place to long" (p 0.22, stance down)
- "Stage 4 downtrend, and indicators all red. Nothing here excites me" (p 0.22, stance down)

These are implicit calls. The stance is in the framing; the tweet never says "will go up". My instructions carry
"be strict, when in doubt answer no", and that phrase is what pushes them down to 0.2. Qwen misses the same
tweets, so this is a prompt problem before it is a model problem.

### What Jev adds (20 extras at p ≥ 0.3)

Chart captions ("#Bitcoin $40k🚀" plus an image, "The Daily Downtrend is over" plus an image), macro narratives with
no price claim (national-debt tweets come back as GOLD BUY LONG), and one tweet about another asset that happens to
carry a #Bitcoin tag ($GME → BTC BUY). The SYSTEM prompt's NOT-a-call list names all three cases. In the typed
decomposition they are strings inside `instructions`, and the noul does not weigh them as hard as a text model
reading the same prompt does.

## How much do two frontier models even agree?

Before grading anything against Opus I wanted to know what "right" looks like. The first gold set was labeled by
Claude Fable 5.1 in an interactive session. I re-labeled its 105 English tweets with Opus 5.5
(`scripts/frontier_ceiling.py`):

| Comparison | n | recall | precision | F1 | direction | horizon | cell |
|---|---|---|---|---|---|---|---|
| Opus 5.5 vs Fable 5.1 | 105 | 1.00 | 0.93 | 0.96 | 0.92 | 0.92 | 0.85 |
| Jev @0.3 vs Fable | 87 | 1.00 | 0.78 | 0.88 | 1.00 | 0.57 | 0.57 |
| Jev @0.3 vs Opus | 87 | 0.88 | 0.78 | 0.82 | 1.00 | 0.43 | 0.43 |
| Qwen vs Fable | 105 | 0.85 | 0.92 | 0.88 | 1.00 | 0.73 | 0.73 |
| Qwen vs Opus | 105 | 0.79 | 0.92 | 0.85 | 1.00 | 0.82 | 0.82 |

The two frontier models agree on is_call for 99 % of tweets (the one disagreement is a halving-cycle progress bar).
On direction and horizon they disagree on 2 of the 13 calls they share: NEUTRAL versus BUY on a two-sided level, and
LONG versus MEDIUM on an undated cycle claim. So roughly 0.92 on direction and 0.92 on horizon is the ceiling, and
nobody should be graded against 1.00. This holdout only has 7 to 14 calls depending on who you ask, so quote the
400-tweet numbers above, not these.

## What I take from it

Jev is the best is_call model I have. At p ≥ 0.2 it finds 87 % of the reference call tweets at 0.912 accuracy,
against 75 % and 0.892 for the 35B I run in production. It does that in a quarter of a second per tweet, for six
thousandths of a cent, about 90 times faster than the local GPU path. I went in treating it as a cheap filter and
came out with it beating the classifier at the classifier's first job.

On direction Jev is at the ceiling. 0.93 to 0.94 agreement with Opus on the 400, 1.00 with both frontier models on
the holdout, and it never swapped BUY for SELL. For "which way" alone, Jev is enough.

Horizon is where it slips, and the reason is structural rather than a capability gap. 0.80 against Qwen's 0.85 and
the 0.92 ceiling, with the loss concentrated in MEDIUM. MEDIUM in my rules means "an undated expectation with no
level game and no structural argument". It is defined by what it is not, and a three-way choice question has no way
to say "neither of the other two". I want to try two nouls instead, "is this a level or pattern trade?" and "is this
a structural, multi-year thesis?", with MEDIUM as the answer when both come back low. I have not run that yet.

The threshold belongs to the prompt. "Be strict, when in doubt answer no" pushes implicit calls to p ≈ 0.2, and on
this set 0.2 beats 0.3 on every end-to-end number. The production gate should move to 0.2: hybrid recall goes from
0.65 to 0.70 and the GPU sees 15 more tweets per 400.

None of this lets Jev replace the text model, and it has nothing to do with accuracy. There is no quote, which is
the evidence column on the audit page, and no price target, which earns or loses credit in scoring. Jev decides
whether a tweet is worth reading closely; the text model does the close reading. Until this week that split was an
assumption. Now it has numbers.

Jev and Qwen also disagree with each other about as much as either disagrees with Opus
(F1 0.78, direction 0.92, horizon 0.82 between them). Flagging calls where Jev's stance differs from Qwen's direction
costs nothing and would surface exactly the two-sided and support-level cases that trip both.

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
SYSTEM prompt in the query, so the reference labels come from the same rules as every other labeler.
`jev_answers.py` reads `TYPESAFE_API_KEY` from `.env`.

## Still to do

- The two-noul horizon shape for Jev.
- Gate threshold 0.3 → 0.2 in `src/gate.py`, then re-measure the daily pass rate.
- A Jev-only matrix from the full corpus (about 10 minutes and $3) next to the production matrix, as a talk
  artifact. The audit page has to tolerate NULL quotes first.
- The 400-set reference is one frontier model's opinion. Fable versus Opus says about 8 % of direction and horizon
  labels are judgment calls, so a second frontier pass on the 400 would give the ceiling on the same set.
