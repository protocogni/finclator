# Finclator

Trust-weighted influencer sentiment → a 3×3 **Buy / Neutral / Sell** matrix for **BTC, GOLD, SPX** across
**SHORT (0–3 mo), MEDIUM (3–12 mo), LONG (1–5 y)**.

Tweets-first: every explicit, falsifiable call in an influencer's tweets is extracted with its quote, evaluated
against price history when the horizon matures, and rolled into a shrunk trust score per (account, asset, horizon).
The matrix is the trust × √confidence × recency weighted vote of the roster's live calls — one vote per account per
cell (its latest call in the window), N/A below 3 voters.

## Pipeline

```
roster.yaml ─► fetch.py (twitterapi.io advanced_search, last_fetch_at watermark) ─► tweets ─► prefilter.py (asset regex, EN/TR)
   ─► classify.py (Claude, JSON calls + quote) ─► calls
prices.py (Yahoo daily closes) ─► evaluate.py (vol-scaled threshold) ─► outcomes ─► score.py (shrinkage) ─► trust
   ─► matrix.py ─► data/matrix.json
```

Storage: Postgres (Neon) via `DATABASE_URL`. One `python -m src.run` per day (launchd).

## Setup

```bash
/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
echo TWITTERAPI_IO_KEY=... >> .env      # tweet fetch
echo ANTHROPIC_API_KEY=... >> .env      # classifier (API mode); the initial backfill was labeled interactively
.venv/bin/python -m src.run             # or --no-fetch / --no-classify
```

## Files

| file | role |
|---|---|
| `roster.yaml` | fixed account list with school + language; edit here to add/remove |
| `src/prefilter.py` | non-reply tweets naming an asset → eligible for the LLM (~14% of tweets) |
| `src/classify.py` | strict call extraction; `export`/`import` subcommands for interactive labeling |
| `src/evaluate.py` | maturity 90/365/730 d; NEUTRAL band = 0.5σ·√days per asset |
| `src/score.py` | `(hits + 5) / (n + 10)`; PARTIAL (off by one step) = 0.5 hit |
| `src/matrix.py` | half-life = window/3, hard cutoff at window; `N/A` when total weight < 0.3 |
| `data/labels_backfill.jsonl` | audit trail of the 334-tweet interactive backfill |

## Roadmap

- Roster audit (`call density` from a 200-tweet sample) to grow to ~30 accounts
- GitHub Actions weekly cron committing `matrix.json`
- Static dashboard + TradingView Pine indicator fed from `matrix.json`
