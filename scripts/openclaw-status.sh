#!/usr/bin/env bash
export PATH="/root/.nvm/versions/node/v22.22.3/bin:$PATH"
TOKEN=$(cat /root/.openclaw/gateway.token 2>/dev/null || true)
echo "gateway: $(systemctl --user is-active openclaw-gateway.service 2>/dev/null)"
ss -lntp | grep 18789 || echo "port 18789: down"
echo "auth profiles:"
openclaw models auth list 2>&1 | head -40
if [[ -n "$TOKEN" ]]; then
  echo "models:"
  curl -sS http://127.0.0.1:18789/v1/models -H "Authorization: Bearer $TOKEN" | head -c 400
  echo
fi
