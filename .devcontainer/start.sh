#!/usr/bin/env bash
# Start the backend (:8000) and the studio (:5173) in the background.
# Runs on every codespace start; safe to rerun (a server that is already running is left alone).
#
#   bash .devcontainer/start.sh          # studio = built frontend (vite preview), fast to load
#   bash .devcontainer/start.sh dev      # studio = vite dev server with hot reload, for editing the frontend
#   bash .devcontainer/start.sh restart  # stop both servers, then start them again
#
# Logs: /tmp/latticeflow-backend.log and /tmp/latticeflow-frontend.log
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.venvs/latticeflow"
MODE="${1:-preview}"

BACKEND_PAT="uvicorn app.main:app"
FRONTEND_PAT="vite.*--port 5173"
running() { pgrep -f "$1" >/dev/null 2>&1; }

if [ "$MODE" = "restart" ]; then
  pkill -f "$BACKEND_PAT" 2>/dev/null || true
  pkill -f "$FRONTEND_PAT" 2>/dev/null || true
  sleep 1
  MODE="${2:-preview}"
fi

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "Setup has not finished (no $VENV). Run: bash .devcontainer/setup.sh" >&2
  exit 1
fi

if running "$BACKEND_PAT"; then
  echo "Backend already running on :8000"
else
  echo "Starting backend on :8000 (log: /tmp/latticeflow-backend.log)"
  cd "$ROOT/backend"
  setsid nohup "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000 \
    >/tmp/latticeflow-backend.log 2>&1 < /dev/null &
fi

if running "$FRONTEND_PAT"; then
  echo "Studio already running on :5173"
else
  cd "$ROOT/frontend"
  if [ "$MODE" = "dev" ]; then
    CMD=(npx vite --host 127.0.0.1 --port 5173 --strictPort)
  else
    [ -f dist/index.html ] || npm run build
    CMD=(npx vite preview --host 127.0.0.1 --port 5173 --strictPort)
  fi
  echo "Starting studio on :5173 ($MODE; log: /tmp/latticeflow-frontend.log)"
  setsid nohup "${CMD[@]}" >/tmp/latticeflow-frontend.log 2>&1 < /dev/null &
fi

echo "The backend loads MACE and runs a warm-up before accepting connections (up to a minute on first start)."
echo "Open the forwarded port 5173 (Ports tab) to use the studio."
