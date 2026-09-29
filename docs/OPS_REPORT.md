# Daily ops email

`src/report.py`, fired by the Vercel cron in `vercel.json` (`30 12 * * *` UTC = 08:30 ET, after the 06:00 ET
pipeline on the operator Mac has deployed) at `GET /panel/report`.

**Sections, in order:** verdict (🟢/🟡/🔴 in the subject) → problems (only if any) → pipeline freshness (matrix.json,
site.json, newest tweet, fetch watermark, gate, classification, outcomes, trust, prod deploy) → last 24 h activity with
deltas vs the prior 24 h → published 3×3 matrix with label changes since the previous report → hit rate vs always-BUY
per horizon + top trust → Web Analytics (24 h / 7 d visitors, countries, paths) → cost (twitterapi.io balance, silent
accounts) → alarm thresholds.

**Alarm thresholds** (constants at the top of `report.py`): matrix/deploy > 30 h, newest tweet > 48 h, pending > 300,
price > 5 d, one account ≥ 50 % of a cell, replies/RTs > 0, balance < $2, site.json vs matrix.json from different runs,
matrix model ≠ active model.

**Auth:** `Authorization: Bearer $CRON_SECRET` (Vercel sends it automatically on cron invocations). `?dry=1` renders the
HTML without sending or persisting. Manual fire:

```
curl -H "Authorization: Bearer $CRON_SECRET" https://finclator.com/panel/report          # sends, returns JSON summary
curl -H "Authorization: Bearer $CRON_SECRET" "https://finclator.com/panel/report?dry=1" > /tmp/r.html
```

**Env (production):** `CRON_SECRET`, `VERCEL_API_TOKEN` (CLI token, for Web Analytics + deployments), `VERCEL_PROJECT_ID`,
`VERCEL_TEAM_ID`, `TWITTERAPI_IO_KEY` (balance), `RESEND_API_KEY` + `MAIL_FROM` (existing), recipients
`OPS_REPORT_EMAIL` (comma-separated; fallback `OWNER_EMAIL`).

**State:** every report is a row in Neon table `reports` (subject, recipients, delivery, problems, labels, html); the
newest row's labels feed the "was BUY" deltas, so they survive
redeploys. Dry runs do not touch it.

**Pitfalls:**
- Resend and api.vercel.com sit behind Cloudflare; a bare `urllib` request (no `User-Agent`) gets 403 `error code: 1010`.
  `_http_json` / `send_email` set one.
- A Neon pooler that drops the socket left psycopg blocked ~15 min; `db._PgConnection` now sets `connect_timeout` and
  TCP keepalives, and `report.run()` retries once on a fresh connection.
- Tests: `tests/test_report.py` against the conftest SQLite fixture (network reads monkeypatched).
