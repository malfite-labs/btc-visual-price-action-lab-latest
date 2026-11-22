#!/usr/bin/env bash
# Start backend (8011), optional shadow worker, and frontend (5174) with flock single-instance.
# Default BTC_LAB_OFFLINE=1 → no real Alpaca WebSocket.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
assert_venv

COMPONENTS="${1:-all}"  # all|backend|shadow|frontend
STAMP="$(date -u +%Y%m%dT%H%M%S%3NZ)-$$"

start_backend() {
  local pf lf
  pf="$(pidfile backend)"; lf="$(lockfile backend)"
  if [[ -f "$pf" ]] && is_pid_running "$(read_pid backend)"; then
    echo "[start] backend already running pid=$(read_pid backend)"
    return 0
  fi
  # Port check
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq ":${API_PORT}$"; then
    echo "[start] ERROR: port ${API_PORT} already listening" >&2
    exit 1
  fi
  echo "[start] backend OFFLINE=${BTC_LAB_OFFLINE} MARKET_MODE=${BTC_LAB_MARKET_MODE} PAPER_AUTOMATION=${PAPER_AUTOMATION_ENABLED}"
  (
    flock -n 9 || { echo "[start] backend flock held" >&2; exit 1; }
    cd "$APP_ROOT"
    nohup "$VENV_PY" -m uvicorn backend.app:app --host "$API_HOST" --port "$API_PORT" \
      >"${LOG_DIR}/backend-${STAMP}.log" 2>"${LOG_DIR}/backend-error-${STAMP}.log" &
    echo $! >"$pf"
    # Keep flock for life of this subshell by waiting on child
    wait $! || true
  ) 9>"$lf" &
  # Give parent a moment then verify
  sleep 1
  if ! wait_http_ok "http://${API_HOST}:${API_PORT}/api/v1/health" 40; then
    echo "[start] backend health failed; see ${LOG_DIR}/backend-error-${STAMP}.log" >&2
    exit 1
  fi
  echo "[start] backend ok pid=$(read_pid backend) http://${API_HOST}:${API_PORT}/"
}

start_shadow() {
  local pf lf
  pf="$(pidfile shadow)"; lf="$(lockfile shadow)"
  if [[ -f "$pf" ]] && is_pid_running "$(read_pid shadow)"; then
    echo "[start] shadow already running pid=$(read_pid shadow)"
    return 0
  fi
  # Shadow consumes local HTTP only; still refuse if offline backend missing
  if ! curl -fsS --max-time 2 "http://${API_HOST}:${API_PORT}/api/v1/health" >/dev/null; then
    echo "[start] shadow requires healthy backend on ${API_PORT}" >&2
    exit 1
  fi
  echo "[start] shadow worker (local HTTP only; existing forward-worker.lock inside engine)"
  (
    flock -n 9 || { echo "[start] shadow flock held" >&2; exit 1; }
    cd "$APP_ROOT"
    # Do NOT pass credentials; worker uses backend HTTP only.
    nohup "$VENV_PY" scripts/run_forward_v26.py \
      >"${LOG_DIR}/shadow-${STAMP}.log" 2>"${LOG_DIR}/shadow-error-${STAMP}.log" &
    echo $! >"$pf"
    wait $! || true
  ) 9>"$lf" &
  sleep 1
  echo "[start] shadow launched pid=$(read_pid shadow)"
}

start_frontend() {
  local pf lf
  pf="$(pidfile frontend)"; lf="$(lockfile frontend)"
  if [[ -f "$pf" ]] && is_pid_running "$(read_pid frontend)"; then
    echo "[start] frontend already running pid=$(read_pid frontend)"
    return 0
  fi
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq ":${UI_PORT}$"; then
    echo "[start] ERROR: port ${UI_PORT} already listening" >&2
    exit 1
  fi
  local vite="${APP_ROOT}/frontend/node_modules/vite/bin/vite.js"
  [[ -f "$vite" ]] || { echo "frontend deps missing; run install.sh" >&2; exit 1; }
  (
    flock -n 9 || { echo "[start] frontend flock held" >&2; exit 1; }
    cd "${APP_ROOT}/frontend"
    nohup node "$vite" --host "$UI_HOST" --port "$UI_PORT" --strictPort \
      >"${LOG_DIR}/frontend-${STAMP}.log" 2>"${LOG_DIR}/frontend-error-${STAMP}.log" &
    echo $! >"$pf"
    wait $! || true
  ) 9>"$lf" &
  sleep 1
  if ! wait_http_ok "http://${UI_HOST}:${UI_PORT}/" 40; then
    echo "[start] frontend failed; see logs" >&2
    exit 1
  fi
  echo "[start] frontend ok http://${UI_HOST}:${UI_PORT}/"
}

case "$COMPONENTS" in
  all) start_backend; start_frontend; echo "[start] shadow NOT auto-started in OFFLINE default; use: $0 shadow";;
  backend) start_backend ;;
  shadow) start_shadow ;;
  frontend) start_frontend ;;
  backend+shadow) start_backend; start_shadow ;;
  *) echo "usage: $0 [all|backend|shadow|frontend|backend+shadow]"; exit 2 ;;
esac
