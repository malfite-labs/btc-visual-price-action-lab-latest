#!/usr/bin/env bash
# 云端独立长稳守护：每个组件一个 supervise 循环（flock 保证单实例）。
# 用法: supervise.sh start|stop|status [backend|frontend|shadow|all]
# 进程退出 → 落盘崩溃证据 → 指数退避(5s..300s) → 重启。不改任何策略状态；
# shadow 的 HALTED_POSITION_GAP 是策略态，保留 status.json，不伪造 resume。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
assert_venv
CRASH_DIR="${LOG_DIR}/crash"; mkdir -p "$CRASH_DIR"
MAX_LOG_BYTES="${BTC_LAB_MAX_LOG_BYTES:-52428800}"   # 50MB
KEEP_DAYS="${BTC_LAB_LOG_KEEP_DAYS:-14}"

rotate() { # $1 file
  local f="$1"
  [[ -f "$f" ]] || return 0
  if (( $(stat -c %s "$f") > MAX_LOG_BYTES )); then
    mv "$f" "$f.$(date +%Y%m%d%H%M%S)"; gzip -q "$f".2* 2>/dev/null || true
  fi
  find "$LOG_DIR" -maxdepth 1 -name '*.log*' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
}

run_child() { # $1 name ; runs in foreground, writes child pidfile
  local n="$1" out="${LOG_DIR}/$1.log" err="${LOG_DIR}/$1-error.log"
  rotate "$out"; rotate "$err"
  case "$n" in
    backend) cd "$APP_ROOT"; "$VENV_PY" -m uvicorn backend.app:app --host "$API_HOST" --port "$API_PORT" >>"$out" 2>>"$err" & ;;
    shadow)  cd "$APP_ROOT"
             until curl -fsS --max-time 2 "http://${API_HOST}:${API_PORT}/api/v1/health" >/dev/null 2>&1; do sleep 5; done
             "$VENV_PY" scripts/run_forward_v26.py >>"$out" 2>>"$err" & ;;
    frontend) cd "${APP_ROOT}/frontend"; node node_modules/vite/bin/vite.js --host "$UI_HOST" --port "$UI_PORT" --strictPort >>"$out" 2>>"$err" & ;;
  esac
  local i=0 cpid=$!; echo "$cpid" >"$(pidfile "$n")"
  # 周期性轮转/健康探测
  while kill -0 "$cpid" 2>/dev/null; do
    sleep 5; (( ++i % 60 == 0 )) && { rotate "$out"; rotate "$err"; }
  done
  wait "$cpid"; return $?
}

loop() { # $1 name
  local n="$1" backoff=5 rc started
  exec 9>"$(lockfile "$n")"
  flock -n 9 || { echo "[supervise] $n already supervised" >&2; exit 1; }
  echo $$ >"${RUN_DIR}/$n.supervisor.pid"
  trap 'p=$(read_pid '"$n"'); [[ -n "$p" ]] && kill "$p" 2>/dev/null; rm -f "$(pidfile '"$n"')" "${RUN_DIR}/'"$n"'.supervisor.pid"; exit 0' TERM INT
  while [[ ! -f "${RUN_DIR}/$n.stop" ]]; do
    started=$(date +%s); rc=0
    ( run_child "$n" ) || rc=$?
    [[ -f "${RUN_DIR}/$n.stop" ]] && break
    { echo "time=$(date -Iseconds) component=$n exit=$rc uptime=$(( $(date +%s)-started ))s"
      tail -n 80 "${LOG_DIR}/$n-error.log" 2>/dev/null; } >"${CRASH_DIR}/$n-$(date +%Y%m%dT%H%M%S).txt"
    (( $(date +%s)-started > 600 )) && backoff=5
    echo "[supervise] $n exit=$rc; restart in ${backoff}s" >>"${LOG_DIR}/supervise.log"
    sleep "$backoff"; backoff=$(( backoff*2 > 300 ? 300 : backoff*2 ))
  done
  rm -f "$(pidfile "$n")" "${RUN_DIR}/$n.supervisor.pid"
}

comps() { [[ "${1:-all}" == all ]] && echo "backend frontend shadow" || echo "$1"; }
case "${1:-}" in
  start) for c in $(comps "${2:-all}"); do rm -f "${RUN_DIR}/$c.stop"
           setsid nohup "$0" _loop "$c" >>"${LOG_DIR}/supervise.log" 2>&1 < /dev/null &
           echo "[supervise] $c started"; [[ $c == backend ]] && wait_http_ok "http://${API_HOST}:${API_PORT}/api/v1/health" 60 || true; done ;;
  stop) for c in $(comps "${2:-all}"); do touch "${RUN_DIR}/$c.stop"; s=$(cat "${RUN_DIR}/$c.supervisor.pid" 2>/dev/null || true)
          [[ -n "$s" ]] && kill -- -"$s" 2>/dev/null || true; sleep 2; rm -f "$(pidfile "$c")" "${RUN_DIR}/$c.supervisor.pid"; echo "[supervise] $c stop requested"; done ;;
  status) for c in backend frontend shadow; do echo "$c supervisor=$(cat "${RUN_DIR}/$c.supervisor.pid" 2>/dev/null || echo -) child=$(read_pid "$c" || true)"; done
          ls -1t "$CRASH_DIR" 2>/dev/null | head -5 ;;
  _loop) loop "$2" ;;
  *) echo "usage: $0 start|stop|status [backend|frontend|shadow|all]"; exit 2 ;;
esac
