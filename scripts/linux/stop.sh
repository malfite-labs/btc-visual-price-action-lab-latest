#!/usr/bin/env bash
# Stop lab processes started by scripts/linux/start.sh (pidfiles under runtime/run).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

stop_one() {
  local name="$1"
  local pf pid
  pf="$(pidfile "$name")"
  pid="$(read_pid "$name")"
  if [[ -z "$pid" ]]; then
    echo "[stop] $name: no pidfile"
    return 0
  fi
  if is_pid_running "$pid"; then
    echo "[stop] $name pid=$pid"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      is_pid_running "$pid" || break
      sleep 0.25
    done
    if is_pid_running "$pid"; then
      echo "[stop] $name still alive; SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
  else
    echo "[stop] $name pid=$pid not running"
  fi
  rm -f "$pf"
}

COMPONENTS="${1:-all}"
case "$COMPONENTS" in
  all) stop_one frontend; stop_one shadow; stop_one backend ;;
  backend|shadow|frontend) stop_one "$COMPONENTS" ;;
  *) echo "usage: $0 [all|backend|shadow|frontend]"; exit 2 ;;
esac
echo "[stop] done"
