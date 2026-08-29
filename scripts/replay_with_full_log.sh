#!/usr/bin/env bash
# HOTFIX-2: рестарт content-воркеров + 24h replay с JSONL-логом.
set -euo pipefail
ROOT=/var/max-repost
cd "$ROOT"
export PYTHONPATH="$ROOT"
TS="$(date -u +%Y%m%d%H%M%S)"
MASTER_LOG="$ROOT/data/editorial/replay_master_${TS}.log"
mkdir -p "$ROOT/data/editorial"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$MASTER_LOG"
}

log "=== replay HOTFIX-2 start ==="

log "STEP: pm2 restart content workers"
for name in max-repost-editorial max-repost-editorial-moderator; do
  log "pm2 restart $name --update-env"
  timeout 120 pm2 restart "$name" --update-env >>"$MASTER_LOG" 2>&1 || true
done

log "STEP: purge stale gate cache (v1 meme/news)"
"$ROOT/.venv/bin/python" - <<'PY' | tee -a "$MASTER_LOG"
import json, sqlite3
from app.config import get_settings, load_dotenv_manual
from editorial.gate_cache import _is_poisoned_verdict
load_dotenv_manual()
conn = sqlite3.connect(get_settings().db_path)
rows = conn.execute("SELECT cache_key, verdict_json FROM editorial_gate_cache").fetchall()
purge = [k for k, v in rows if _is_poisoned_verdict(json.loads(v))]
with conn:
    for k in purge:
        conn.execute("DELETE FROM editorial_gate_cache WHERE cache_key=?", (k,))
print(f"gate_cache purged {len(purge)} of {len(rows)}")
PY

log "STEP: replay 24h"
"$ROOT/.venv/bin/python" -u "$ROOT/scripts/replay_production_24h.py" \
  --slug vnf_editorial --hours 24 \
  2>&1 | tee -a "$MASTER_LOG"

log "=== done ==="
