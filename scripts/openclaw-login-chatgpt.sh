#!/usr/bin/env bash
# Интерактивный вход в ChatGPT/Codex через OpenClaw (нужен TTY / SSH -t)
set -euo pipefail
export PATH="/root/.nvm/versions/node/v22.22.3/bin:$PATH"
export HTTP_PROXY=http://127.0.0.1:10809
export HTTPS_PROXY=http://127.0.0.1:10809
export ALL_PROXY=http://127.0.0.1:10809
export NO_PROXY=localhost,127.0.0.1,::1
export NODE_USE_ENV_PROXY=1
export OPENCLAW_PROXY_URL=http://127.0.0.1:10809

echo "=== OpenClaw ChatGPT login (device code) ==="
echo "Прокси: $HTTPS_PROXY"
echo "Следуй инструкциям в терминале (открой URL и введи код)."
echo

openclaw models auth login --provider openai --device-code --set-default --force

echo
echo "Готово. Проверка профилей:"
openclaw models auth list || true
systemctl --user restart openclaw-gateway.service
sleep 2
systemctl --user is-active openclaw-gateway.service
