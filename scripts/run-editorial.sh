#!/usr/bin/env bash
set -euo pipefail
ROOT=/var/max-repost
cd "$ROOT"
export PYTHONPATH="$ROOT"
if [[ -f /root/.nvm/nvm.sh ]]; then
  # shellcheck disable=SC1091
  source /root/.nvm/nvm.sh
  nvm use 22 >/dev/null 2>&1 || true
fi
export PATH="/root/.nvm/versions/node/v22.22.3/bin:${PATH}"

PY="$ROOT/.venv/bin/python"
if ! "$PY" -c "from playwright.sync_api import sync_playwright" >/dev/null 2>&1; then
  echo "[editorial] playwright python package missing — pip install playwright" >&2
fi
if ! "$PY" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.executable_path; p.stop()" >/dev/null 2>&1; then
  echo "[editorial] installing chromium for playwright…" >&2
  "$PY" -m playwright install chromium || true
fi

exec "$PY" -u workers/editorial_run.py
