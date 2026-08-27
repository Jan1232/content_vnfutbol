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

exec "$ROOT/.venv/bin/python" -u workers/editorial_moderator_bot.py
