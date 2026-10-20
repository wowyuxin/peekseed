#!/usr/bin/env bash
#
# Manage a local peekseed server.
#
#   ./scripts/serve.sh start | stop | restart | status | logs
#
# Configuration: PEEKSEED_DATA, PEEKSEED_HOST, PEEKSEED_PORT, PY.
# A .env file at the project root (next to this script's parent) is loaded as
# defaults, so the same checkout runs on any machine without edits.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# Minimal .env loading (KEY=VALUE, optional export, comments, quotes). Values
# already present in the environment win, so one-shot overrides still work.
load_dotenv() {
  local f="$ROOT/.env" line key val
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    case "$val" in
      \"*\") val="${val:1:${#val}-2}" ;;
      \'*\') val="${val:1:${#val}-2}" ;;
    esac
    if [ -z "${!key+x}" ]; then export "$key=$val"; fi
  done < "$f"
}
load_dotenv

PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
PORT="${PEEKSEED_PORT:-8030}"
HOST="${PEEKSEED_HOST:-127.0.0.1}"
PIDFILE="$ROOT/peekseed.pid"
LOGFILE="$ROOT/peekseed.log"

log() { printf '[peekseed] %s\n' "$*"; }

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

wait_healthy() {
  for _ in $(seq 1 40); do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

start() {
  if is_running; then
    log "already running (pid $(cat "$PIDFILE"))"; status; return 0
  fi
  [ -x "$PY" ] || { log "python not found: $PY (set PY=...)"; return 1; }
  log "starting on $HOST:$PORT"
  nohup "$PY" -m peekseed serve --host "$HOST" --port "$PORT" \
    </dev/null >"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  if wait_healthy; then
    log "started -> http://$HOST:$PORT/"
  else
    log "did not become healthy; see $LOGFILE"; return 1
  fi
}

stop() {
  if is_running; then
    local pid; pid="$(cat "$PIDFILE")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    log "stopped."
  else
    log "not running."
  fi
}

status() {
  if is_running; then
    log "running (pid $(cat "$PIDFILE"))"
    curl -fsS -m 2 "http://127.0.0.1:$PORT/health" \
      && printf '\n' || log "health: not responding"
  else
    log "stopped."
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -f "$LOGFILE" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
