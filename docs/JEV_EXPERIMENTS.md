# Four classifiers, one thousand tweets: what I measured

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
question I had been avoiding, because I assumed the answer was no: can Jev produce the two things the matrix needs,
direction and horizon, on its own? And how does it compare with the model I actually run, the model I used to run,
and a frontier model, all reading the same tweets under the same rules?

## Setup

One thousand tweets, five labelers, one set of rules.

The tweets: 1,000 English originals that mention BTC, gold or the S&P 500, drawn at random from the corpus. 76
accounts, at most 12 per account, spread over 2023 through 2026, none of them in any earlier gold set.
`scripts/sample_tweets.py` writes them in two batches (`data/pending_en.jsonl`, `data/pending_en2.jsonl`) that
`data/pending_en1000.jsonl` concatenates.

The reference: Claude Fable 5.1 with reasoning set to high, given the verbatim classifier SYSTEM prompt from
`src/classify.py`, 40 tweets per request, every quote checked as a substring of its tweet.
`scripts/label_frontier.py` writes `data/labels_en1000_fable51.jsonl`. It answered all 1,000 in 255 seconds: 143 call
tweets (14 %), 151 (tweet, asset) calls, zero malformed rows, zero bad quotes. Fable is the labeled truth until a
better labeler shows up; the numbers below are agreement with Fable, not with the market.

The four models, all with the same SYSTEM prompt:

- Claude Opus 5.5, reasoning high, same script as the reference. `data/labels_en_opus55.jsonl`, 1,000 tweets in
  about 5 minutes across two runs.
- Jev, `jev-latest` (answers as `jev-1.13.0`), direct API at `api.typesafe.ai/v1/systemone`, 32 workers. The SYSTEM
  prompt decomposed into typed questions: `is_call` is a noul (one probability back); for each asset the tweet
  mentions there is a stance choice (none / up / down / neutral) and a horizon choice (SHORT / MEDIUM / LONG).
  Question text in `scripts/score_jev.py`, runner `scripts/jev_answers.py`. 1,000 tweets in 8.5 seconds of wall
  time across two runs, zero errors, 0.24 s median latency, about $0.06 in input tokens.
- Qwen3.6-35B-A3B q4_K_M, the production classifier, on Ollama on an M5 Pro with 64 GB. Production config: 4 tweets
  per request, terse output, thinking off, 8 workers. `scripts/qwen_answers.py`. 1,000 tweets in about 12 minutes,
  77 to 88 tweets a minute.
- Qwen3-30B-A3B instruct-2507 q4_K_M, the model the 35B replaced, same config. 1,000 tweets in 26 minutes, 38 tweets
  a minute.

Scoring is `scripts/compare_four.py`. The unit is one (tweet, asset) call, because that is what the matrix consumes.
Recall is the share of reference calls the model also called. Precision is the share of the model's calls that exist
in the reference. Direction, horizon and cell are agreement on the calls both sides made (cell means both right).
"e2e cell" is the share of reference calls that came out fully right, recall times cell.

Jev counts as having made a call when `p_call` clears the threshold and the stance for that asset is not `none`.
Direction is the stance choice, horizon is the horizon choice.

## Results, reference = Fable 5.1, 1,000 tweets, 151 reference calls

### Per call

| model | calls made | shared | recall | precision | F1 | direction | horizon | cell | e2e cell |
|---|---|---|---|---|---|---|---|---|---|
| Opus 5.5 | 167 | 137 | 0.91 | 0.82 | 0.86 | 0.98 | 0.89 | 0.87 | 0.79 |
| Jev p ≥ 0.2 | 210 | 133 | 0.88 | 0.63 | 0.74 | 0.97 | 0.83 | 0.80 | 0.70 |
| Jev p ≥ 0.3 (the production gate threshold) | 178 | 123 | 0.81 | 0.69 | 0.75 | 0.97 | 0.83 | 0.80 | 0.65 |
| Jev p ≥ 0.4 | 141 | 111 | 0.74 | 0.79 | 0.76 | 0.97 | 0.81 | 0.78 | 0.58 |
| Jev p ≥ 0.5 | 107 | 94 | 0.62 | 0.88 | 0.73 | 0.97 | 0.80 | 0.77 | 0.48 |
| Qwen3.6-35B (production) | 181 | 120 | 0.79 | 0.66 | 0.72 | 0.96 | 0.81 | 0.78 | 0.62 |
| Qwen3-30B (previous) | 188 | 108 | 0.72 | 0.57 | 0.64 | 0.90 | 0.70 | 0.63 | 0.45 |

### Per tweet, is_call only (14 % of tweets are calls in the reference)

| model | accuracy | precision | recall |
|---|---|---|---|
| Opus 5.5 | 0.957 | 0.81 | 0.91 |
| Jev p ≥ 0.2 | 0.913 | 0.64 | 0.90 |
| Jev p ≥ 0.3 | 0.924 | 0.70 | 0.83 |
| Jev p ≥ 0.4 | 0.936 | 0.79 | 0.75 |
| Qwen3.6-35B | 0.921 | 0.69 | 0.81 |
| Qwen3-30B | 0.886 | 0.58 | 0.74 |

### Where the disagreements are (rows = Fable, columns = the model)

Direction, on the calls each model shares with Fable:

| model | BUY → | NEUTRAL → | SELL → |
|---|---|---|---|
| Opus 5.5 | 96 BUY, 0, 0 | 1 BUY, 8 NEUTRAL, 0 | 0, 2 NEUTRAL, 30 SELL |
| Jev @0.3 | 87 BUY, 0, 0 | 0, 7 NEUTRAL, 1 SELL | 1 BUY, 2 NEUTRAL, 25 SELL |
| Qwen3.6-35B | 87 BUY, 0, 0 | 2 BUY, 0, 1 SELL | 0, 2 NEUTRAL, 28 SELL |
| Qwen3-30B | 76 BUY, 0, 1 SELL | 5 BUY, 1 NEUTRAL, 1 SELL | 4 BUY, 0, 20 SELL |

Horizon:

| model | SHORT → | MEDIUM → | LONG → |
|---|---|---|---|
| Opus 5.5 | 51 SHORT, 6 MEDIUM, 0 | 3, 29 MEDIUM, 3 | 0, 3 MEDIUM, 42 LONG |
| Jev @0.3 | 52 SHORT, 4 MEDIUM, 0 | 9 SHORT, 18 MEDIUM, 4 | 2, 2, 32 LONG |
| Qwen3.6-35B | 47 SHORT, 5 MEDIUM, 0 | 3, 21 MEDIUM, 4 | 1, 10 MEDIUM, 29 LONG |
| Qwen3-30B | 35 SHORT, 11 MEDIUM, 0 | 6, 19 MEDIUM, 4 | 5, 6, 22 LONG |

Opus, Jev and the 35B between them flip BUY and SELL twice in 380 shared calls. The 30B does it five times in 108,
and it also reads 5 of 7 NEUTRAL calls as BUY, so it does not really get two-sided level conditionals ("above X
bullish, below X bearish"), which the other three handle.

Horizon errors have a signature per model. Jev pushes MEDIUM into SHORT (9 of 31 reference-MEDIUM calls; its
predicted mix is 100/31/47 against Fable's 61/39/51). The 35B pushes LONG into MEDIUM (10 of 40). Opus is
symmetric and small. The 30B is off in every direction at once.

### By asset (recall / precision / direction / horizon)

| asset | ref calls | Opus 5.5 | Jev @0.3 | Qwen3.6-35B | Qwen3-30B |
|---|---|---|---|---|---|
| BTC | 95 | 0.92 / 0.89 / 0.98 / 0.87 | 0.86 / 0.74 / 0.96 / 0.82 | 0.81 / 0.73 / 0.95 / 0.83 | 0.84 / 0.62 / 0.86 / 0.70 |
| GOLD | 26 | 0.85 / 0.76 / 1.00 / 0.86 | 0.81 / 0.66 / 1.00 / 0.86 | 0.85 / 0.49 / 0.95 / 0.73 | 0.58 / 0.44 / 1.00 / 0.73 |
| SPX | 30 | 0.93 / 0.70 / 0.96 / 0.96 | 0.67 / 0.57 / 0.95 / 0.85 | 0.70 / 0.68 / 1.00 / 0.81 | 0.43 / 0.50 / 1.00 / 0.69 |

S&P 500 calls are the hard ones for everything below the frontier. Fable counts positioning statements ("added a
small handful of longs", "still holding our SPY short") and index-move commentary with a stance as calls; the
non-frontier models read many of those as observations.

### By year (recall / precision / direction / horizon)

| year | tweets | ref calls | Opus 5.5 | Jev @0.3 | Qwen3.6-35B | Qwen3-30B |
|---|---|---|---|---|---|---|
| 2023 | 139 | 14 | 1.00 / 0.74 / 1.00 / 0.71 | 0.86 / 0.52 / 1.00 / 0.75 | 0.93 / 0.68 / 1.00 / 0.69 | 0.79 / 0.50 / 0.91 / 0.55 |
| 2024 | 290 | 36 | 0.83 / 0.75 / 0.97 / 0.90 | 0.78 / 0.64 / 0.96 / 0.93 | 0.81 / 0.62 / 1.00 / 0.79 | 0.58 / 0.55 / 1.00 / 0.76 |
| 2025 | 290 | 48 | 0.94 / 0.87 / 1.00 / 0.84 | 0.81 / 0.68 / 0.97 / 0.72 | 0.77 / 0.69 / 0.95 / 0.81 | 0.71 / 0.53 / 0.85 / 0.68 |
| 2026 | 281 | 53 | 0.91 / 0.86 / 0.96 / 0.98 | 0.83 / 0.81 / 0.95 / 0.89 | 0.77 / 0.67 / 0.93 / 0.85 | 0.79 / 0.66 / 0.88 / 0.74 |

No drift by year worth talking about. The 2023 precision dip is 14 reference calls; one extra call moves it 5 points.

### How much the models agree with each other (F1 on calls / direction / horizon on shared)

| | Opus 5.5 | Jev @0.3 | Qwen3.6-35B | Qwen3-30B |
|---|---|---|---|---|
| Opus 5.5 | | 0.72 / 0.95 / 0.86 | 0.70 / 0.97 / 0.79 | 0.61 / 0.90 / 0.74 |
| Jev @0.3 | 0.72 / 0.95 / 0.86 | | 0.74 / 0.91 / 0.77 | 0.67 / 0.89 / 0.75 |
| Qwen3.6-35B | 0.70 / 0.97 / 0.79 | 0.74 / 0.91 / 0.77 | | 0.64 / 0.92 / 0.72 |

Jev and the production 35B agree with each other (F1 0.74) about as much as either agrees with Opus. A `dir-disagree`
audit flag where Jev's stance differs from the 35B's direction costs nothing and would catch the two-sided and
support-level cases that trip both.

### The production hybrid: Jev gate, then Qwen3.6 labels

| gate | passed to the GPU | recall | precision | direction | horizon | e2e cell |
|---|---|---|---|---|---|---|
| none (35B alone) | 1,000 (100 %) | 0.79 | 0.66 | 0.96 | 0.81 | 0.62 |
| Jev p ≥ 0.2 | 202 (20 %) | 0.77 | 0.79 | 0.96 | 0.82 | 0.60 |
| Jev p ≥ 0.3 (current) | 171 (17 %) | 0.72 | 0.79 | 0.95 | 0.82 | 0.56 |

At 0.2 the gate costs 2 points of recall against the ungated 35B and cuts GPU work by 80 %. At 0.3, the threshold
in production today, it costs 7 points for 83 %. Precision goes up under either gate, because Jev and the 35B make
different false positives and the conjunction removes some of each.

### What nobody finds

Four of the 151 reference calls are missed by all four models:

- "Bitcoin is the ultimate hedge against chaos. $BTC" (Fable: BTC BUY LONG)
- "None of us own enough hard assets." (GOLD BUY LONG)
- "Welcome to an unhinged inflationary era. Game on." (GOLD BUY LONG)
- "2025 will be a big year. Deregulation is bullish as fk." (SPX BUY MEDIUM)

Reading them, I would not have called two of these. Fable is generous with thesis statements that never mention a
price, and that generosity is part of the reference. Eight more reference calls are found by Opus and by nothing
else; those are the implicit calls where the stance lives in the framing.

## What I take from it

Opus 5.5 against Fable 5.1 sets the ceiling: F1 0.86, direction 0.98, horizon 0.89 on 137 shared calls. Two frontier
models reading the same rules still disagree on 1 call in 10 about whether it is a call at all, and on 1 in 9 about
horizon. I keep that in mind when reading every row below; 1.00 was never on the table.

Jev is the best non-frontier model I have on the thing it is built for. At p ≥ 0.2 it finds 90 % of the reference
call tweets, against 81 % for the 35B I run in production and 74 % for the 30B before it, and it does that in a
quarter of a second per tweet for six thousandths of a cent. I went in treating it as a cheap filter and it came out ahead
of the classifier at the classifier's first job, which I did not expect.

On direction Jev sits with the frontier. 0.97 against Opus's 0.98, and one BUY/SELL flip in 123 shared calls.
For "which way" alone, Jev is enough.

Horizon is where the gap is, and I think the cause is the question shape, not the model. 0.83 against the 35B's
0.81 and the 0.89 ceiling, with the loss concentrated in MEDIUM. MEDIUM in my rules means "an undated expectation
with no level game and no structural argument". It is defined by what it is not, and a three-way choice question
has no way to say "neither of the other two". I want to try two nouls instead, "is this a level or pattern trade?"
and "is this a structural, multi-year thesis?", with MEDIUM as the answer when both come back low. I have not run
that yet.

Precision is Jev's real weakness, not horizon. 0.63 at the threshold that gives the best recall. The extras are
chart captions with an image, macro narratives with no price claim (national-debt tweets come back as GOLD BUY LONG),
and tweets about another asset that carry a #Bitcoin tag. The SYSTEM prompt's NOT-a-call list names all three. In the
typed decomposition they are strings inside `instructions`, and the noul does not weigh them as hard as a text model
reading the same prompt does.

The threshold belongs to the prompt. "Be strict, when in doubt answer no" pushes implicit calls to p ≈ 0.2, and on
this set 0.2 beats 0.3 on every end-to-end number. The production gate should move to 0.2: hybrid recall goes from
0.72 to 0.77 and the GPU sees 3 % more tweets.

The 35B over the 30B was the right swap, and now I can say by how much: F1 0.72 against 0.64, direction 0.96 against
0.90, horizon 0.81 against 0.70, and twice the throughput. The 30B's five BUY/SELL flips in 108 calls would have put
wrong-sign votes into the matrix.

None of this lets Jev replace the text model, and it has nothing to do with accuracy. There is no quote, which is
the evidence column on the audit page, and no price target, which earns or loses credit in scoring. Jev decides
whether a tweet is worth reading closely and the text model does the close reading. I had been running that split
on an assumption; now I know what each half costs and what it buys.

## Reproduce

```
PYTHONPATH=. .venv/bin/python scripts/sample_tweets.py 400 8 data/pending_en.jsonl
PYTHONPATH=. .venv/bin/python scripts/sample_tweets.py 600 12 data/pending_en2.jsonl --exclude data/pending_en.jsonl --seed 20260930
cat data/pending_en.jsonl data/pending_en2.jsonl > data/pending_en1000.jsonl
PYTHONPATH=. FRONTIER_MODEL=claude-fable-5-1 .venv/bin/python scripts/label_frontier.py data/pending_en1000.jsonl data/labels_en1000_fable51.jsonl 40 4
PYTHONPATH=. FRONTIER_MODEL=claude-opus-5-5 .venv/bin/python scripts/label_frontier.py data/pending_en1000.jsonl data/labels_en_opus55.jsonl 40 4
PYTHONPATH=. .venv/bin/python scripts/jev_answers.py data/pending_en.jsonl en
PYTHONPATH=. .venv/bin/python scripts/jev_answers.py data/pending_en2.jsonl en2
PYTHONPATH=. .venv/bin/python scripts/qwen_answers.py data/pending_en.jsonl en          # Ollama up
PYTHONPATH=. .venv/bin/python scripts/qwen_answers.py data/pending_en2.jsonl en2
PYTHONPATH=. FINCLATOR_MODEL=qwen3:30b-a3b-instruct-2507-q4_K_M .venv/bin/python scripts/qwen_answers.py data/pending_en1000.jsonl en1000 qwen30b
PYTHONPATH=. .venv/bin/python scripts/compare_four.py
```

`label_frontier.py` runs the frontier model through `hermes chat -m <model> --reasoning high` with the SYSTEM
prompt in the query, so both frontier label sets come from the same rules as every other labeler. It is resumable:
ids already in the output file are skipped. `jev_answers.py` reads `TYPESAFE_API_KEY` from `.env`.

## Earlier run: 400 tweets against Opus 5.5

The first pass used the first 400 of these tweets with Opus 5.5 as the reference (`scripts/compare_labelers.py en`).
Same ranking, same shape: Jev p ≥ 0.2 recall 0.85 / direction 0.94 / horizon 0.81, the 35B recall 0.74 / direction
0.97 / horizon 0.85. The 1,000-tweet Fable numbers above supersede it. The 105-tweet English holdout where Opus and
Fable agreed on direction and horizon at 0.92 (`scripts/frontier_ceiling.py`) is likewise superseded by the 137
shared calls here.

## Still to do

- The two-noul horizon shape for Jev.
- Gate threshold 0.3 → 0.2 in `src/gate.py`, then re-measure the daily pass rate.
- A Jev-only matrix from the full corpus (about 10 minutes and $3) next to the production matrix, as a talk
  artifact. The audit page has to tolerate NULL quotes first.
- Adjudicate the 30 calls where Opus and Fable disagree. Whichever way they go, that is the noise floor of the
  reference.
