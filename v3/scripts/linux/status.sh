#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

echo "APP_ROOT=$APP_ROOT"
echo "RUN_DIR=$RUN_DIR"
echo "OFFLINE=${BTC_LAB_OFFLINE} MARKET_MODE=${BTC_LAB_MARKET_MODE}"
echo "PAPER_AUTOMATION_ENABLED=${PAPER_AUTOMATION_ENABLED}"
for name in backend shadow frontend; do
  pid="$(read_pid "$name")"
  if [[ -n "$pid" ]] && is_pid_running "$pid"; then
    echo "$name: RUNNING pid=$pid"
  else
    echo "$name: STOPPED pidfile=${pid:-none}"
  fi
done
echo "--- ports ---"
ss -ltnp 2>/dev/null | grep -E ":${API_PORT}\\s|:${UI_PORT}\\s" || echo "(no listeners on ${API_PORT}/${UI_PORT})"
echo "--- health ---"
curl -fsS --max-time 2 "http://${API_HOST}:${API_PORT}/api/v1/health" 2>/dev/null || echo "backend health: unreachable"
echo
# Shadow status file if present
if [[ -f "${APP_ROOT}/docs/reports/abc-forward-v26/status.json" ]]; then
  echo "--- shadow status.json (head) ---"
  head -c 2000 "${APP_ROOT}/docs/reports/abc-forward-v26/status.json" || true
  echo
fi
