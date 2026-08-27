#!/usr/bin/env bash
set -euo pipefail
cd /var/max-repost/calorie-bot
exec /var/max-repost/calorie-bot/.venv/bin/python -m bot.main
