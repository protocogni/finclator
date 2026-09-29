# LinkedIn post: finclator (first public mention)

Post from the personal profile. Angle: the production pipeline as a search + LLM system, models
named. No always-Buy comparison. Attach `marketing/architecture.png`.

---

Finance influencers make a lot of calls. Nobody keeps score. I built a system that does: it extracts every explicit, falsifiable market call from what they post, grades it against price once the horizon has passed, and turns the graded record into a Buy/Neutral/Sell matrix for Bitcoin, gold and the S&P 500 at three horizons.

The production pipeline, layer by layer.

Collect. About a hundred accounts, originals only (replies and reposts rejected at the API call, the response filter and the DB insert). Incremental fetch against a per-account watermark; years of history per account, because a two-year horizon cannot be graded on last week's data.

Retrieve. Real calls are a small minority of a feed. Stage one is a regex prefilter that keeps anything naming an asset at all, tuned for recall. Stage two is TypeSafe's Jev, a decision model that answers one typed question, "is this a falsifiable call?", with a calibrated probability instead of text. Threshold p ≥ 0.2, 32 workers, about 3,000 posts a minute. One post in five goes on.

Extract. Qwen3.6-35B-A3B (q4_K_M, Ollama, an M5 Pro with 64 GB), four posts per request, thinking off, about 115 posts a minute. Per post it returns a structured record: asset, direction, horizon, the exact quote that justifies the label, and a price target if the author named one. The quote is validated as a substring of the post and becomes the evidence column on the audit page. Labels are stored per (post, asset, model), so a second model's labels sit beside the first and never overwrite them.

Measure. A thousand English posts labeled by Claude Fable 5.1 as the reference, then every other labeler under the same system prompt, scored per (post, asset) call. Opus 5.5 against Fable sets the ceiling: F1 0.86, direction 0.98, horizon 0.89; two frontier models still disagree on 1 call in 10 about whether it is a call at all. Jev, at the gate's own job, finds 88% of the reference calls; Qwen alone finds 79%. Direction is at the frontier for both (0.97 and 0.96). Jev then Qwen cuts GPU work by 80% for two points of recall, and precision rises from 0.66 to 0.79 because their false positives differ.

Grade. Daily closes for BTC-USD, gold front-month and the S&P 500. A call matures at 90, 365 or 730 days. The return is compared against a flat band of half a standard deviation of that asset's move over that horizon. A stated target, if sane relative to entry, adds or removes 0.25. Trust per (account, asset, horizon) is (hits + 5) / (n + 10), so one lucky call cannot outrank a 50-for-100 record, and it is computed point-in-time, so a historical matrix never sees the future.

Vote. One vote per account per cell, its latest call in the window, weight = trust × √confidence × recency with half-life a third of the window. Below three voters the cell is N/A.

Public side: the live 3×3, the method page with formulas, per-account trust grids, and an audit of every call with its quote, entry and exit close, and result.

Finclator. https://finclator.com

---

## "Tell your network what your post is about"

Finance influencers make a lot of calls and nobody keeps score. A search-then-extract pipeline (regex → Jev gate → Qwen3.6-35B) that grades every call against price and weights the graded record into a live Buy/Neutral/Sell matrix, every cell traceable to a quote.

## Pre-written replies

Q: Why not just ask a frontier model for everything?
A: Cost and auditability. Tens of thousands of posts per pass, daily. The local model is free at the margin and the frontier models are what I score against, not what runs. Full numbers: docs/JEV_EXPERIMENTS.md in the repo.

Q: Why a gate in front of the LLM instead of a better prompt?
A: Jev answers a typed question with a probability in a quarter of a second and finds more calls than the 35B does on its own. The two models' false positives don't overlap, so the conjunction is more precise than either. The threshold belongs to the prompt: "be strict" pushes implicit calls to p ≈ 0.2, which is why it sits there.

Q: Why the quote?
A: Without it the label is unfalsifiable. The audit page highlights the quote inside the post, links the post, and shows the price path. You can disagree with a label in ten seconds.

Q: Who is on the roster?
A: A fixed list I curated by school (macro, crypto, quant, technical, goldbug). Trust does the filtering, not me: noisy accounts shrink to the prior and get outweighed.
