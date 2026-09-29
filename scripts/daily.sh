#!/bin/bash
# Daily pipeline on the operator Mac, run by launchd job com.finclator.daily (install: scripts/install_daily.py).
# fetch → classify (local Ollama, batched) → prices → evaluate → score → matrix → audit → pine → site,
# then deploy the public numbers to Vercel and commit the small artefacts. Log: data/daily.log (launchd captures stdout).
# audit.html is NOT committed by this job (430k lines; the panel renders audit live from the DB).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PYTHONPATH=. FINCLATOR_WORKERS=8 FINCLATOR_TERSE=1 FINCLATOR_BATCH_SIZE=4
export FINCLATOR_MODEL_BASE_URL=http://localhost:11434/v1
echo "== daily start $(date -u +%FT%TZ)"
.venv/bin/python scripts/backup_db.py || echo "backup FAILED (continuing)"
if ! curl -sf http://localhost:11434/api/tags >/dev/null; then
  echo "ollama not up — starting a transient server"
  OLLAMA_NUM_PARALLEL=8 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 nohup ollama serve >>data/ollama.log 2>&1 &
  for i in $(seq 1 30); do curl -sf http://localhost:11434/api/tags >/dev/null && break; sleep 2; done
fi
if ! .venv/bin/python -m src.run; then
  echo "== daily FAILED in src.run $(date -u +%FT%TZ)"; exit 1
fi
vercel deploy --prod --yes >>data/deploy.log 2>&1 && echo "deployed" || echo "deploy FAILED (see data/deploy.log)"
git add data/matrix.json public/site.json tradingview/finclator.pine
git commit -qm "daily: matrix + site.json $(date -u +%F)" || echo "nothing to commit"
git pull -q --rebase --autostash origin main || echo "rebase FAILED — resolve by hand"
git push -q origin main && echo "pushed" || echo "push failed"
echo "== daily done $(date -u +%FT%TZ)"
