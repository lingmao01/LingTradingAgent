#!/usr/bin/env bash
# Vibe-Trading one-click launcher.
#
# Installs missing dependencies (backend via uv, frontend via npm), then starts
# the FastAPI backend and the Vite dev server in the foreground. Each service
# also writes to its own log file under .vibe-dev/logs; the lines are streamed
# here with [backend] / [frontend] prefixes.
# Press Ctrl+C once to stop both services.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
LOG_DIR="${VIBE_START_LOG_DIR:-$ROOT/.vibe-dev/logs}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8899}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5899}"
BACKEND_READY_TIMEOUT="${BACKEND_READY_TIMEOUT:-90}"
FRONTEND_READY_TIMEOUT="${FRONTEND_READY_TIMEOUT:-60}"

# PyPI index used by uv. Precedence: UV_DEFAULT_INDEX, then PYPI_INDEX, then the
# Tsinghua mirror (fast inside mainland China).
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-${PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}}"

BACKEND_PID=""
FRONTEND_PID=""
FOLLOW_PIDS=""
OPEN_BROWSER="${VIBE_START_OPEN_BROWSER:-0}"

usage() {
  cat <<'USAGE'
Usage: ./start.sh [--open]

One-click launcher for Vibe-Trading. It installs missing dependencies, starts
the backend (FastAPI) and the frontend (Vite dev server), streams both logs
here, and stops both services when you press Ctrl+C.

Options:
  --open                   Open the frontend in your browser once it is ready
  -h, --help               Show this help

Environment variables:
  VIBE_START_OPEN_BROWSER  Set to 1 to always open the frontend in a browser
  BACKEND_HOST             Backend bind host (default: 127.0.0.1)
  BACKEND_PORT             Backend port (default: 8899)
  FRONTEND_HOST            Frontend bind host (default: 127.0.0.1)
  FRONTEND_PORT            Frontend port (default: 5899)
  BACKEND_READY_TIMEOUT    Seconds to wait for the backend (default: 90)
  FRONTEND_READY_TIMEOUT   Seconds to wait for the frontend (default: 60)
  PYPI_INDEX               PyPI mirror used by uv (default: Tsinghua mirror)
  VENV_DIR                 Backend virtualenv path (default: <repo>/.venv)
  VIBE_START_LOG_DIR       Directory for service logs (default: .vibe-dev/logs)
USAGE
}

info() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

backend_url() {
  printf 'http://%s:%s' "$BACKEND_HOST" "$BACKEND_PORT"
}

frontend_url() {
  printf 'http://%s:%s' "$FRONTEND_HOST" "$FRONTEND_PORT"
}

backend_ready() {
  curl -fsS --max-time 2 "$(backend_url)/health" >/dev/null 2>&1
}

frontend_ready() {
  curl -fsS --max-time 2 "$(frontend_url)/" >/dev/null 2>&1
}

# Opens the frontend in the default browser when enabled (--open). Never fails
# the run: a missing opener just prints the URL to visit manually.
open_frontend() {
  if [ "$OPEN_BROWSER" != "1" ]; then
    return 0
  fi
  local url
  url="$(frontend_url)"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  else
    info "[open] No 'open' or 'xdg-open' command found; visit $url manually."
    return 0
  fi
  info "[open] Frontend opened in your browser: $url"
}

prefixed() {
  local tag="$1"
  awk -v tag="$tag" '{ printf "[%s] %s\n", tag, $0; fflush() }'
}

# Streams a service log file to this terminal with a [name] prefix.
follow_log() {
  local file="$1"
  local tag="$2"
  tail -n +1 -f "$file" 2>/dev/null | prefixed "$tag" &
  FOLLOW_PIDS="$FOLLOW_PIDS $!"
}

require_cmd() {
  local name="$1"
  local hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    fail "$name not found. $hint"
  fi
}

check_prerequisites() {
  require_cmd curl "Install curl and retry."
  require_cmd uv "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"
  require_cmd node "Install Node.js 22+ from https://nodejs.org"
  require_cmd npm "Install Node.js 22+ from https://nodejs.org"

  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "$node_major" -lt 22 ]; then
    fail "Node.js 22+ required, found $(node --version). Install from https://nodejs.org"
  fi
}

ensure_backend_deps() {
  local stamp_file="$VENV_DIR/.vibe-deps-stamp"

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "[setup] Creating backend virtualenv at $VENV_DIR (uv, Python 3.11)"
    uv venv --python 3.11 "$VENV_DIR"
  fi

  if [ -f "$stamp_file" ] && [ "$stamp_file" -nt "$ROOT/pyproject.toml" ] && [ -x "$VENV_DIR/bin/vibe-trading" ]; then
    info "[setup] Backend dependencies up to date; skipping install."
    return 0
  fi

  info "[setup] Installing backend dependencies with uv"
  info "[setup] PyPI index: $UV_DEFAULT_INDEX"
  uv pip install --python "$VENV_DIR/bin/python" -e "$ROOT"
  touch "$stamp_file"
}

ensure_env_file() {
  if [ ! -f "$ROOT/agent/.env" ] && [ -f "$ROOT/agent/.env.example" ]; then
    cp "$ROOT/agent/.env.example" "$ROOT/agent/.env"
    info "[setup] Created agent/.env from agent/.env.example"
    info "[setup] Edit agent/.env to set your LLM provider API key before chatting."
  fi
}

ensure_frontend_deps() {
  if [ -x "$ROOT/frontend/node_modules/.bin/vite" ]; then
    info "[setup] Frontend dependencies up to date; skipping install."
    return 0
  fi
  info "[setup] Installing frontend dependencies with npm (first run can take a while)"
  (cd "$ROOT/frontend" && npm install)
}

start_backend() {
  local log="$LOG_DIR/backend.log"
  : > "$log"
  (
    cd "$ROOT"
    export PYTHONPATH="$ROOT/agent"
    exec "$VENV_DIR/bin/python" -c 'import cli, sys; raise SystemExit(cli.main(sys.argv[1:]))' serve \
      --host "$BACKEND_HOST" --port "$BACKEND_PORT"
  ) >> "$log" 2>&1 &
  BACKEND_PID=$!
  follow_log "$log" backend
}

start_frontend() {
  local log="$LOG_DIR/frontend.log"
  : > "$log"
  (
    cd "$ROOT/frontend"
    export VITE_API_URL="${VITE_API_URL:-$(backend_url)}"
    exec node_modules/.bin/vite --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --strictPort
  ) >> "$log" 2>&1 &
  FRONTEND_PID=$!
  follow_log "$log" frontend
}

wait_until_ready() {
  local name="$1"
  local pid="$2"
  local ready_fn="$3"
  local timeout="$4"
  local i=0

  info "[boot] Waiting for $name to be ready ..."
  while [ "$i" -lt "$timeout" ]; do
    if "$ready_fn"; then
      info "[boot] $name is ready."
      return 0
    fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      printf 'ERROR: %s exited during startup. Check the [%s] log lines above or %s/%s.log\n' \
        "$name" "$name" "$LOG_DIR" "$name" >&2
      return 1
    fi
    i=$((i + 1))
    sleep 1
  done
  printf 'ERROR: %s was not ready after %ss. Check the [%s] log lines above or %s/%s.log\n' \
    "$name" "$timeout" "$name" "$LOG_DIR" "$name" >&2
  return 1
}

stop_pid() {
  local pid="$1"
  if [ -z "$pid" ]; then
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  local i=0
  while [ "$i" -lt 50 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.1
    i=$((i + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  trap - HUP INT TERM EXIT
  if [ -n "$BACKEND_PID" ] || [ -n "$FRONTEND_PID" ] || [ -n "$FOLLOW_PIDS" ]; then
    info ""
    info "[stop] Stopping services ..."
    stop_pid "$FRONTEND_PID"
    stop_pid "$BACKEND_PID"
    BACKEND_PID=""
    FRONTEND_PID=""
    local pid
    for pid in $FOLLOW_PIDS; do
      kill -TERM "$pid" 2>/dev/null || true
    done
    FOLLOW_PIDS=""
    # Anything left at this point is a log follower (tail); reap it.
    sleep 0.3
    local child
    for child in $(pgrep -P "$$" 2>/dev/null); do
      kill -TERM "$child" 2>/dev/null || true
    done
    info "[stop] Both services stopped."
  fi
}

# Signal handler: clean up and leave (bash would otherwise resume the script).
handle_signal() {
  local code="$1"
  cleanup
  info ""
  exit "$code"
}

print_urls() {
  info "  Frontend:  $(frontend_url)"
  info "  Backend:   $(backend_url)"
  info "  API docs:  $(backend_url)/docs"
  info "  Logs:      $LOG_DIR"
}

monitor() {
  while :; do
    if [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      printf 'ERROR: backend exited; stopping the remaining services.\n' >&2
      return 1
    fi
    if [ -n "$FRONTEND_PID" ] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
      printf 'ERROR: frontend exited; stopping the remaining services.\n' >&2
      return 1
    fi
    sleep 1
  done
}

main() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      --open)
        OPEN_BROWSER=1
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
    shift
  done

  info "Vibe-Trading launcher"
  info "  Repository:    $ROOT"
  info "  Backend:       $(backend_url)"
  info "  Frontend:      $(frontend_url)"
  info ""

  check_prerequisites
  mkdir -p "$LOG_DIR"
  ensure_backend_deps
  ensure_env_file
  ensure_frontend_deps
  info ""

  if backend_ready; then
    info "[boot] Backend already running at $(backend_url); reusing it."
    BACKEND_PID=""
  else
    info "[boot] Starting backend ..."
    start_backend
  fi

  if frontend_ready; then
    info "[boot] Frontend already running at $(frontend_url); reusing it."
    FRONTEND_PID=""
  else
    info "[boot] Starting frontend ..."
    start_frontend
  fi

  if [ -z "$BACKEND_PID" ] && [ -z "$FRONTEND_PID" ]; then
    info ""
    print_urls
    info ""
    open_frontend
    info "Both services were already running; nothing to manage. Exiting."
    exit 0
  fi

  if ! wait_until_ready backend "$BACKEND_PID" backend_ready "$BACKEND_READY_TIMEOUT"; then
    exit 1
  fi
  if ! wait_until_ready frontend "$FRONTEND_PID" frontend_ready "$FRONTEND_READY_TIMEOUT"; then
    exit 1
  fi

  info ""
  print_urls
  info ""
  open_frontend
  info "Press Ctrl+C to stop both services."
  info ""

  monitor
}

trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
trap cleanup EXIT

main "$@"