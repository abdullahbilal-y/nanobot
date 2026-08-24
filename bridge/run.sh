#!/usr/bin/env bash
# Start/stop the WhatsApp bridge as a detached process.
#
# Why this exists: `pkill -f "node dist/index.js"` also matches the shell
# running the command, so a combined stop-and-start over SSH kills itself
# before it can start anything. The bracket trick below stops the pattern
# matching its own command line.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${BRIDGE_LOG:-/tmp/bridge.log}"
PATTERN='[n]ode dist/index'

running() { pgrep -f "$PATTERN" >/dev/null 2>&1; }

case "${1:-status}" in
  start)
    if running; then
      echo "already running (pid $(pgrep -f "$PATTERN" | head -1))"
      exit 0
    fi
    cd "$DIR" || exit 1
    [ -f "$LOG" ] && mv -f "$LOG" "$LOG.prev"
    setsid nohup node dist/index.js > "$LOG" 2>&1 < /dev/null &
    sleep 3
    if running; then
      echo "started (pid $(pgrep -f "$PATTERN" | head -1)), log: $LOG"
    else
      echo "FAILED to start; last lines of $LOG:"
      tail -20 "$LOG" 2>/dev/null
      exit 1
    fi
    ;;
  stop)
    if ! running; then
      echo "not running"
      exit 0
    fi
    pkill -f "$PATTERN"
    sleep 3
    running && { echo "still running, sending KILL"; pkill -9 -f "$PATTERN"; sleep 2; }
    running && echo "FAILED to stop" || echo "stopped"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if running; then
      echo "running (pid $(pgrep -f "$PATTERN" | head -1))"
      grep -E "Connected to WhatsApp|Connection closed|Scan this QR" "$LOG" 2>/dev/null | tail -3
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
