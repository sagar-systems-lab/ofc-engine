#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ ! -f .venv/bin/activate ]]; then
  echo "FAIL: activate the project's local Python .venv first; no installs performed." >&2
  exit 2
fi
source .venv/bin/activate

echo "[1/6] Full keyless Python regression"
NODE_OPTIONS=--experimental-default-type=module python -m pytest -q

echo "[2/6] Frontend syntax and saved-advisory contracts"
node --check src/ofc_engine/web/copilot-workspace.js
node --check src/ofc_engine/web/copilot-proof.js
node scripts/verify_copilot_advisory.mjs

echo "[3/6] Start isolated localhost preview; no Qloo/model credentials needed"
python -m ofc_engine.hosted_preview \
  --bind 127.0.0.1 --port 18080 > /tmp/ofc-final-stability-browser.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

ready=0
for _ in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:18080/readyz >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "FAIL: localhost preview not healthy; see /tmp/ofc-final-stability-browser.log" >&2
  exit 1
fi

echo "[4/6] Taste Explorer desktop/mobile and no-redraw browser checks"
python scripts/verify_taste_explorer_browser.py \
  --base-url http://127.0.0.1:18080

echo "[5/6] Full Copilot desktop/mobile, recovery, replay, quota safety"
python scripts/copilot_workspace_smoke.py \
  --base-url http://127.0.0.1:18080

echo "[6/6] Exact named-seed clarification and historic blocked recovery"
python scripts/verify_clarification_browser.py \
  --base-url http://127.0.0.1:18080

echo "EXACT_SEED_RELEASE_LOCAL_PASS (mocked browser responses; zero Qloo/Gemini requests)"
