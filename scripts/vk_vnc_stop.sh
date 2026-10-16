#!/usr/bin/env bash
set -euo pipefail
PID_FILE=/var/max-repost/data/vk_vnc.pids
if [[ ! -f "$PID_FILE" ]]; then
  echo "Сессия не запущена"
  exit 0
fi
while read -r pid; do
  [[ -n "$pid" ]] || continue
  kill "$pid" 2>/dev/null || true
done < "$PID_FILE"
sleep 0.5
while read -r pid; do
  [[ -n "$pid" ]] || continue
  kill -9 "$pid" 2>/dev/null || true
done < "$PID_FILE"
rm -f "$PID_FILE"
echo "VNC/Chrome остановлены. Профиль сессии остался в data/vk_browser"
