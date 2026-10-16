#!/usr/bin/env bash
set -euo pipefail
ROOT=/var/max-repost
cd "$ROOT"
export PYTHONPATH="$ROOT"
exec "$ROOT/.venv/bin/python" -u -m predlozka.guard "$@"
