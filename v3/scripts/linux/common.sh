#!/usr/bin/env bash
# Shared paths and offline-safe defaults for BTC Visual Price Action Lab on Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Prefer sibling runtime/{run,logs} when deployed under .../runtime/app
if [[ "$(basename "$(dirname "$APP_ROOT")")" == "runtime" ]]; then
  RUNTIME_ROOT="$(cd "${APP_ROOT}/.." && pwd)"
  RUN_DIR="${RUNTIME_ROOT}/run"
  LOG_DIR="${RUNTIME_ROOT}/logs"
else
  RUN_DIR="${APP_ROOT}/.run"
  LOG_DIR="${APP_ROOT}/logs"
fi
mkdir -p "$RUN_DIR" "$LOG_DIR" "${APP_ROOT}/.run" "${APP_ROOT}/logs"

API_PORT="${BTC_LAB_API_PORT:-8011}"
UI_PORT="${BTC_LAB_UI_PORT:-5174}"
API_HOST="${BTC_LAB_API_HOST:-127.0.0.1}"
UI_HOST="${BTC_LAB_UI_HOST:-127.0.0.1}"

VENV_PY="${APP_ROOT}/.venv/bin/python"
# Default OFFLINE: do not start real Alpaca WS. Authorized cutover: BTC_LAB_OFFLINE=0 + credentials.
BTC_LAB_OFFLINE="${BTC_LAB_OFFLINE:-1}"
export PAPER_AUTOMATION_ENABLED="${PAPER_AUTOMATION_ENABLED:-false}"
export VISION_ENABLED="${VISION_ENABLED:-false}"
export VISION_CALLS_AUTHORIZED="${VISION_CALLS_AUTHORIZED:-false}"
export RUN_SIMULATION="${RUN_SIMULATION:-false}"

if [[ "$BTC_LAB_OFFLINE" == "1" || "$BTC_LAB_OFFLINE" == "true" ]]; then
  export ALPACA_CREDENTIALS_FILE="${ALPACA_CREDENTIALS_FILE:-/home/box/btc-visual-price-lab/secrets/alpaca-offline-empty.env}"
  export BTC_LAB_MARKET_MODE="OFFLINE_NO_ALPACA"
else
  export ALPACA_CREDENTIALS_FILE="${ALPACA_CREDENTIALS_FILE:-/home/box/btc-visual-price-lab/secrets/alpaca-cloud-independent.env}"
  export BTC_LAB_MARKET_MODE="LIVE_CREDENTIALS_FILE"
fi

export VITE_API_BASE="${VITE_API_BASE:-http://${API_HOST}:${API_PORT}/api/v1}"

pidfile() { echo "${RUN_DIR}/$1.pid"; }
lockfile() { echo "${RUN_DIR}/$1.lock"; }

is_pid_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local f; f="$(pidfile "$1")"
  [[ -f "$f" ]] || { echo ""; return 0; }
  tr -d ' \n\r\t' <"$f"
}

wait_http_ok() {
  local url="$1" tries="${2:-30}" i
  for ((i=0;i<tries;i++)); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

assert_venv() {
  [[ -x "$VENV_PY" ]] || { echo "Missing venv at $VENV_PY; run scripts/linux/install.sh" >&2; exit 1; }
}
