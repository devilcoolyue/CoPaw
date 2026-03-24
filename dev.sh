#!/usr/bin/env bash
# CoPaw dev server — start / restart from source
# Usage: ./dev.sh [--port 8088] [--reload]

set -euo pipefail
cd "$(dirname "$0")"

PORT="${COPAW_PORT:-8088}"
RELOAD=""
LOG_LEVEL="info"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)   PORT="$2"; shift 2 ;;
    --reload) RELOAD="--reload"; shift ;;
    --debug)  LOG_LEVEL="debug"; shift ;;
    *)        echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Kill all existing processes on the same port
PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "Stopping existing processes on port $PORT (pid: $(echo $PIDS | tr '\n' ' '))..."
  echo "$PIDS" | xargs kill 2>/dev/null || true
  # Wait until port is actually freed
  for i in 1 2 3 4 5; do
    if ! lsof -ti "tcp:$PORT" &>/dev/null; then
      break
    fi
    sleep 1
  done
  # Force kill if still alive
  PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
  if [[ -n "$PIDS" ]]; then
    echo "Force killing remaining processes..."
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

echo "Starting CoPaw dev server on :$PORT ..."
exec python3 -m uvicorn copaw.app._app:app \
  --host 127.0.0.1 \
  --port "$PORT" \
  --log-level "$LOG_LEVEL" \
  $RELOAD
