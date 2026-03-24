#!/usr/bin/env bash
# CoPaw dev restart — rebuild frontend + restart backend
# Usage: ./dev_restart.sh [--skip-fe] [--port 8088] [--debug]

set -euo pipefail
cd "$(dirname "$0")"

PORT="${COPAW_PORT:-8088}"
LOG_LEVEL="info"
SKIP_FE=false
PYTHON="${COPAW_PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-fe) SKIP_FE=true; shift ;;
    --port)    PORT="$2"; shift 2 ;;
    --debug)   LOG_LEVEL="debug"; shift ;;
    *)         echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---- 1. Build frontend ----
if [[ "$SKIP_FE" == false ]]; then
  echo "==> Building frontend..."
  (cd console && npm run build)
  echo "==> Frontend build done."
else
  echo "==> Skipping frontend build."
fi

# ---- 2. Stop existing server ----
PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "==> Stopping existing processes on port $PORT (pid: $(echo $PIDS | tr '\n' ' '))..."
  echo "$PIDS" | xargs kill 2>/dev/null || true
  for i in 1 2 3 4 5; do
    if ! lsof -ti "tcp:$PORT" &>/dev/null; then break; fi
    sleep 1
  done
  # Force kill if still alive
  PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
  if [[ -n "$PIDS" ]]; then
    echo "    Force killing remaining processes..."
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

# ---- 3. Start backend ----
echo "==> Starting CoPaw server on :$PORT ..."
nohup "$PYTHON" -m uvicorn copaw.app._app:app \
  --host 127.0.0.1 \
  --port "$PORT" \
  --log-level "$LOG_LEVEL" \
  > /tmp/copaw.log 2>&1 &
SERVER_PID=$!

# ---- 4. Health check ----
echo "    Waiting for server (pid: $SERVER_PID)..."
for i in 1 2 3 4 5 6; do
  sleep 1
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null || echo "000")
  if [[ "$HTTP_CODE" == "200" ]]; then
    echo "==> Server is up! http://127.0.0.1:$PORT/"
    echo "    Log: /tmp/copaw.log"
    exit 0
  fi
done

echo "==> WARNING: Server did not respond with 200 within 6s."
echo "    Check /tmp/copaw.log for details."
tail -10 /tmp/copaw.log
exit 1
