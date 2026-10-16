#!/usr/bin/env bash
set -euo pipefail
ROOT=/var/max-repost
cd "$ROOT"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
export PYTHONPATH="$ROOT"
exec "$ROOT/.venv/bin/uvicorn" app.main:app \
  --host "${ADMIN_HOST:-0.0.0.0}" \
  --port "${ADMIN_PORT:-8790}" \
  --proxy-headers
