#!/usr/bin/env bash
# Браузер VK на VPS + noVNC: залогиньтесь сами, сессия останется на сервере.
set -euo pipefail
ROOT=/var/max-repost
DATA="$ROOT/data"
PROF="$DATA/vk_browser"
PASS_FILE="$DATA/vk_vnc.pass"
PID_FILE="$DATA/vk_vnc.pids"
DISPLAY_NUM="${VK_VNC_DISPLAY:-99}"
VNC_PORT="${VK_VNC_PORT:-5901}"
WEB_PORT="${VK_NOVNC_PORT:-6081}"
export DISPLAY=":${DISPLAY_NUM}"

mkdir -p "$PROF" "$DATA"
chmod 700 "$DATA"

if [[ -f "$PID_FILE" ]]; then
  echo "Сессия уже запущена. Откройте http://$(curl -4 -s --max-time 3 ifconfig.me):${WEB_PORT}/vnc.html"
  echo "Пароль: $(cat "$PASS_FILE" 2>/dev/null || echo '(см. data/vk_vnc.pass)')"
  exit 0
fi

if [[ ! -f "$PASS_FILE" ]]; then
  openssl rand -hex 4 > "$PASS_FILE"
  chmod 600 "$PASS_FILE"
fi
PASS="$(cat "$PASS_FILE")"
x11vnc -storepasswd "$PASS" "${PASS_FILE}.vnc" >/dev/null

Xvfb "$DISPLAY" -screen 0 1400x900x24 -ac -nolisten tcp >/tmp/vk-xvfb.log 2>&1 &
echo $! >> "$PID_FILE"
sleep 0.4
openbox >/tmp/vk-openbox.log 2>&1 &
echo $! >> "$PID_FILE"
sleep 0.3

google-chrome-stable \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-infobars \
  --no-first-run \
  --no-default-browser-check \
  --start-maximized \
  --lang=ru \
  --user-data-dir="$PROF" \
  --window-size=1400,900 \
  "https://m.vk.ru/" >/tmp/vk-chrome.log 2>&1 &
echo $! >> "$PID_FILE"
sleep 0.8

x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -forever -shared \
  -rfbauth "${PASS_FILE}.vnc" -xkb -noxdamage \
  >/tmp/vk-x11vnc.log 2>&1 &
echo $! >> "$PID_FILE"
sleep 0.4

websockify --web=/usr/share/novnc "0.0.0.0:${WEB_PORT}" "127.0.0.1:${VNC_PORT}" \
  >/tmp/vk-novnc.log 2>&1 &
echo $! >> "$PID_FILE"

IP="$(curl -4 -s --max-time 5 ifconfig.me || echo SERVER)"
echo "Откройте: http://${IP}:${WEB_PORT}/vnc.html"
echo "Пароль VNC: ${PASS}"
echo "Войдите в VK аккаунтом-админом групп. Когда закончите — напишите «вошёл»."
