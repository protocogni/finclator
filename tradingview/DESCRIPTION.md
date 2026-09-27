# Finclator: Finfluencer Sentiment Oscillator

_Paste this as the script description when publishing. Visibility: **Protected** (public, source hidden). Category: Oscillators; tags: sentiment, bitcoin, gold, spx, social._

---

**What it shows**

A consensus reading of what a fixed roster of ~100 finance accounts on X are calling for **Bitcoin, Gold and the S&P 500**, over three horizons — SHORT (0–3 months), MEDIUM (3–12 months) and LONG (1–5 years) — weighted by each account's measured track record.

- **Histogram** — composite sentiment for the chart's asset, −1 (all trusted calls bearish) to +1 (all bullish). Green above +0.15, red below −0.15, grey in the neutral band. Horizon weights are inputs.
- **Three lines** — the SHORT / MEDIUM / LONG readings separately.
- **3×3 table** (top right) — all nine asset × horizon cells with the net score and the number of calls behind each, chart's asset highlighted.
- **Alerts** — composite or any horizon flipping BUY / NEUTRAL / SELL.

The asset is detected from the ticker (BTC, XAU/GC/GOLD/GLD, SPX/SPY/ES/US500) and can be overridden in the inputs.

**How it is computed**

1. Original tweets (no replies, no retweets) from the roster are read by a language model that extracts only **explicit, falsifiable calls**: an asset, a direction, a horizon and, where stated, a price target. Mood, news, past moves and questions are not calls.
2. When a call's horizon matures (3 / 12 / 24 months) it is scored against the actual price move: correct, partial (called a move, market flat) or wrong (opposite move — or a neutral call and a big move either way), with a bonus/penalty for a stated price target being reached.
3. Each account earns a **trust score** per asset and horizon from its matured calls, shrunk toward 0.5 for small samples, so a lucky streak does not outrank a long record.
4. Current calls are aggregated **one vote per account per cell** (its latest call inside the window) with weight = trust × √(the model's confidence) × recency decay, and `net = (buy − sell) / total`; a cell with fewer than three voters is n/a. The call counts in the table are all calls inside the window, not votes.

The history plotted on the chart is **point-in-time**: every past value uses only the calls and outcomes that were known on that date, so scrolling back is an honest backtest, not hindsight.

**What it is not**

Not a price prediction and not financial advice. It measures what a specific set of public commentators are saying and how reliable they have been; it says nothing about whether they will be right this time. Sentiment across the roster has been bullish for most of 2023–2026, so a BUY reading is common and a flip to NEUTRAL or SELL is the more informative event.

**Data & updates**

Roster, calls, outcomes and per-account trust are published for verification (link in signature). The script embeds weekly snapshots and is republished after each weekly update; users who favourite it receive updates automatically. Instruments used for scoring: BTC-USD, gold front-month futures (GC=F), S&P 500 index (^GSPC).
