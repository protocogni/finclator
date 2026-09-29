# finclator — agent context

Finfluencer tweets → explicit, falsifiable market calls → evaluated against price when the horizon matures →
per-account trust → a 3×3 BUY/NEUTRAL/SELL matrix (BTC, GOLD, SPX × SHORT 0–3 mo / MEDIUM 3–12 mo / LONG 1–5 y).
Surfaces: **finclator.com** (public landing + method page, gated admin panel), `data/audit.html`, and
`tradingview/finclator.pine` (publishing on hold). Spec: `specs/001-influencer-trust-scores/`. GitHub **protocogni/finclator**
(public; `main` is the only branch that matters, collaborators are Read-only → fork + PR). Branch `main`
(`001-influencer-trust-scores` kept in sync). Standing product decisions live in the
`social-sentiment-trading-signals` skill; site/panel/Vercel ops in its `references/website-ops.md`.

## State of play (2026-09-23)
- **Backlog fully classified by `qwen3.6-local:35b-a3b-q4_K_M`** (51.6k relevant tweets → 11.5k calls → 7.7k matured
  outcomes, 73 accounts scored). The 30B labels and the frontier labels (`claude-fable-5.1/interactive`) coexist as
  other model dimensions. **Never purge `calls`/`classified_by`/`trust`/`gate` rows of a non-active model.**
- **Jev gate is live for new tweets only** (`src/gate.py`, TypeSafe direct API, `TYPESAFE_API_KEY`, ~3k tw/min,
  32 workers, `p_call ≥ 0.2` since 2026-09-30 — the 1,000-tweet eval in `docs/JEV_EXPERIMENTS.md` showed 0.3 cost 7 pts
  of recall, 0.2 costs 2): `classify_pending` gates every pending tweet, blocked ones are stored as non-calls,
  passing ones (~20 %) go to Qwen. Labels stay under the Qwen tag (user decision: no relabel of the backlog;
  `FINCLATOR_GATE_TAG=1` would fork a `+jev` dimension, `FINCLATOR_GATE=0` disables the gate). `calls.gate_p`
  carries the probability; Audit flags `low-gate`.
- **Roster underperforms always-BUY at every horizon** (measured on the plain Qwen labels: SHORT 55 % vs 62 %,
  MEDIUM 71 % vs 81 %, LONG 76 % vs 83 %) — `score.hit_rates()`; shown on Matrix tab, `/method`, `site.json`. Say so
  when discussing "skill".
- **Database is Postgres (Neon, Vercel team Protocogni Labs)** via `DATABASE_URL` in `.env`; `data/finclator.db` is an
  untracked cold backup of the pre-migration state. `db.connect()` falls back to SQLite only when `DATABASE_URL` is unset.
- Site live at **https://finclator.com** (DNS at Cloudflare, cert issued, www → apex). Resend mail from
  `admin@finclator.com` verified end-to-end (sign-in links deliver).
- **Scheduled: launchd `com.finclator.daily` 06:00 local** runs `scripts/daily.sh` (src.run → `vercel deploy` →
  commit matrix.json/site.json/pine → push). Log `data/daily.log`. Install/refresh: `scripts/install_daily.py [--run-now|--remove]`.

## Dev environment
- Python ≥3.11 (venv is 3.14 at `.venv`); `.venv/bin/pip install -e ".[dev]"` (or `scripts/bootstrap.sh`, which also
  **wipes and rebuilds the DB** — don't run it casually).
- Run modules as `.venv/bin/python -m src.<mod>`; scripts as `PYTHONPATH=. .venv/bin/python scripts/<x>.py`.
- `.env` (gitignored): `DATABASE_URL` (Neon pooled), `TWITTERAPI_IO_KEY` (fetch), `TYPESAFE_API_KEY` (Jev gate;
  also a Sensitive Vercel var), `ANTHROPIC_API_KEY` (API-mode
  classifier), `RESEND_API_KEY` + `MAIL_FROM` (mail). Local classifier needs
  `FINCLATOR_MODEL_BASE_URL=http://localhost:11434/v1` (Ollama; model tag defaults to `models.DEFAULT_MODEL`, batch-4).
- Admin UI: launchd `com.finclator.admin` serves http://127.0.0.1:8787 (tabs: Progress, Matrix, Accounts, Audit,
  Architecture, Tables, `/api/status`). After editing `src/admin.py`/`audit.py`: `.venv/bin/python scripts/restart_admin.py`
  (kills strays holding the port, kickstarts the job). Verify with `scripts/check_pages.py`; the browser tool blocks
  localhost. Same pages are served hosted at finclator.com/panel by `api/panel.py`.
- Node deps (`package.json`, pnpm) exist only for `api/auth.js` (`@vercel/blob` ≥2, `resend`).

## Commands
- Daily pipeline: `.venv/bin/python -m src.run [--no-fetch] [--no-classify]` (fetch → classify → prices → evaluate →
  score → matrix → audit → pine → site). Writes straight to Neon; the hosted panel reflects it without a deploy.
  `public/site.json` (landing numbers) still needs `vercel deploy --prod --yes` to go live. `scripts/daily.sh` does all of it.
- Page audit: `PYTHONPATH=. .venv/bin/python scripts/check_pages.py [https://finclator.com/panel "fc_session=…"] [--public]`
  — 200, no Traceback/None/nan, one `<nav>`, ≤1 "sign out", <10 s. Cookie: copy `fc_session` from the browser
  (SESSION_SECRET is a Sensitive Vercel var; `vercel env pull` returns "").
- Commit+push in one gated-safe call: `bash scripts/gitc.sh "<msg>" [paths…]`.
- Deploy: `vercel deploy --prod --yes` (project linked to `protocogni/finclator`). Env: `vercel env ls`.
- Backfill tweets: `PYTHONPATH=. .venv/bin/python scripts/backfill.py --workers 8` (resumable; verify with `pgrep -f backfill.py`).
  Accounts the search index under-serves (0 stored tweets although public): `scripts/probe_handles.py` then
  `scripts/walk_handles.py <handle>…` (timeline cursor walk, `fetch.walk_timeline`).
- Classify backlog locally (resumable, newest→oldest, rebuilds trust/matrix/audit per batch): `scripts/start_run.sh`
  (launchd jobs for Ollama + `scripts/classify_run.py` + admin; survive the desktop session) / `scripts/stop_run.sh`.
  Nothing is ever re-classified (`classified_by(tweet_id, model)` PK). `classify_pending` batches BATCH_SIZE tweets per
  request on the Ollama path (`tests/test_classify_batch.py` asserts the code path; `scripts/smoke_batch.py` proves it live).
- Model agreement vs frontier labels: `PYTHONPATH=. .venv/bin/python scripts/agreement.py [N] [data/labels_holdout.jsonl]`,
  then `scripts/agreement_diff.py {is_call|dir|hor}` to read disagreements.
- After changing `src/prefilter.py`: `PYTHONPATH=. .venv/bin/python scripts/reprefilter.py` (re-tags every stored tweet).
- Prefilter cases: `PYTHONPATH=. .venv/bin/python tests/test_prefilter.py` — a plain script printing `failures: N`.
  Unit tests: `.venv/bin/python -m pytest tests -q` (admin chrome, batch routing, hit_rates, matrix concentration).
  Lint: `.venv/bin/ruff check .` (line length 110, rules E/F/W/I/B; 3 B905 in src/ + 12 in scripts/ are known).
- SQLite → Postgres (re)migration: `DATABASE_URL=<unpooled> PYTHONPATH=. .venv/bin/python scripts/migrate_to_pg.py --yes`
  (truncates target, COPY, checksums, asserts identical matrix).

## Conventions (observed)
- Module per pipeline stage in `src/` (`fetch`, `prefilter`, `classify`, `prices`, `evaluate`, `score`, `matrix`, `audit`,
  `pine`, `site`, `admin`, `models`); each has `if __name__ == "__main__"` and takes a `conn` from `db.connect()`.
- **SQL is written in SQLite dialect everywhere**; `db._PgConnection.to_pg()` rewrites `?`, `INSERT OR IGNORE/REPLACE`,
  `datetime('now')`, `instr()` for Postgres. New idioms must be portable or added to `to_pg` — no `sum(<bool expr>)`,
  no `WHERE <int col>` without `=1`, no `PRAGMA`, no `sqlite_master` (use `db.tables/columns/primary_key`). Rows support
  `r["col"]`, `r[0]`, `.keys()` on both backends; Postgres `Decimal` arrives as `float`.
- Logging is `db.log(msg)` (UTC-timestamped, stdout + `data/pipeline.log`, tailed live by the admin page) — not bare `print`.
- Schema in `db.SCHEMA`; additive column migrations are entries in `db.MIGRATIONS` (applied on every `connect()` for
  both backends). Destructive migrations go in `scripts/migrate_*.py`.
- Model is a first-class dimension: `calls` unique per `(tweet_id, asset, model)`, `trust` keyed by model, published model
  = `models.active_model()` (`FINCLATOR_ACTIVE_MODEL` → `FINCLATOR_MODEL` → Fable backfill tag). Never sum across models.
- One classifier `SYSTEM` prompt in `src/classify.py` shared by every backend (Anthropic, Ollama native, OpenAI-compat).
  Ollama's `/v1` route silently drops `num_ctx`; the `:11434` branch uses `/api/chat` for that reason.
- Admin pages are f-string HTML built into a `B` list, `html.escape` as `e`, tables `class=sortable`, numeric headers get
  `title=` tooltips or a `<p class=help>` legend. Audit page is rendered inside the admin chrome via `admin._page`.
  **Hosted chrome goes through `admin.CHROME` (ContextVar: prefix/who/readonly/refresh) and in-body links through
  `admin._u(path)` — never wrap/monkey-patch `admin._page`** (a wrapper surviving a failed request nested the sign-out
  block once per request). `_readonly()` hides local-only widgets (log, rebuild link, process cards).
- Public pages: numbers only from `public/site.json`; no stack/model/infra names on `/` or `/method`.
- Commit messages: one line, imperative, semicolon-separated scope list (see `git log --oneline`).

## Pitfalls
- Originals only: replies/RTs are rejected at the API call, the response filter, and the DB insert. `replies_in_db` / `rts_in_db` on Progress must stay 0.
- Horizons are 90/365/730 d maturity, defined once in `evaluate.MATURITY_DAYS`; do not add a second definition.
- Trust must be point-in-time (`score.compute(as_of=)`) for anything historical (Pine history, backtests), and
  `matrix.build(today=)` must also bound `called_at` — without it the Pine history was flat BUY.
- Python `\b` is ASCII-only — Turkish regex uses the explicit `_B0/_B1` boundaries in `prefilter.py`; `html.unescape` first.
- `write_file` on an existing file is refused unless read in the same turn; `read_file` output is line-prefixed — never write it back.
- Prompt-tuning the classifier: score a **held-out** frontier-labeled set (`data/labels_holdout.jsonl`), not only the tuning set.
- Yahoo `query1.finance.yahoo.com/v8/finance/chart/<sym>` needs a browser UA; instruments are BTC-USD, `GC=F`, `^GSPC`.
- `node_modules/`, `.vercel/`, `.env.local`, `data/finclator.db` are gitignored — keep them so (the DB was 82 MB in git).
- `vercel env add` needs one env per call and no output redirection around it; `--sensitive` is refused on `development`.
