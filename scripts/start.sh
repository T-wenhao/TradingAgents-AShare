#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${TA_RUN_DIR:-"$ROOT_DIR/.run"}"
PID_DIR="$RUN_DIR/pids"
LOG_DIR="$RUN_DIR/logs"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5175}"
FRONTEND_API_URL="${VITE_API_URL:-"http://$BACKEND_HOST:$BACKEND_PORT"}"
AUTO_INSTALL="${AUTO_INSTALL:-0}"

BACKEND_PID_FILE="$PID_DIR/backend.pid"
FRONTEND_PID_FILE="$PID_DIR/frontend.pid"
SUPERVISOR_PID_FILE="$PID_DIR/start.pid"

BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

BACKEND_PID=""
FRONTEND_PID=""
TAIL_PID=""
SHUTTING_DOWN=0

info() {
  printf '[start] %s\n' "$*"
}

fail() {
  printf '[start] ERROR: %s\n' "$*" >&2
  exit 1
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

is_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

pid_from_file() {
  local file="$1"
  [ -f "$file" ] || return 1
  tr -dc '0-9' < "$file"
}

ensure_not_running() {
  local name="$1"
  local file="$2"
  local pid
  pid="$(pid_from_file "$file" || true)"
  if is_running "$pid"; then
    fail "$name 已在运行，PID=$pid。请先关闭之前启动的脚本。"
  fi
  rm -f "$file"
}

ensure_port_free() {
  local label="$1"
  local port="$2"
  if has_command lsof && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "$label 端口 $port 已被占用。请释放端口，或设置 ${label}_PORT 后重试。"
  fi
}

terminate_tree() {
  local pid="$1"
  local child
  is_running "$pid" || return 0
  if has_command pgrep; then
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
      terminate_tree "$child"
    done
  fi
  kill -TERM "$pid" >/dev/null 2>&1 || true
}

cleanup() {
  local code=$?
  if [ "$SHUTTING_DOWN" -eq 1 ]; then
    exit "$code"
  fi
  SHUTTING_DOWN=1

  info "正在关闭本次启动的服务..."
  [ -n "$TAIL_PID" ] && terminate_tree "$TAIL_PID"
  [ -n "$FRONTEND_PID" ] && terminate_tree "$FRONTEND_PID"
  [ -n "$BACKEND_PID" ] && terminate_tree "$BACKEND_PID"
  rm -f "$SUPERVISOR_PID_FILE" "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"
  info "已退出。"
  exit "$code"
}

trap cleanup EXIT INT TERM HUP

mkdir -p "$PID_DIR" "$LOG_DIR"
printf '%s\n' "$$" > "$SUPERVISOR_PID_FILE"

has_command uv || fail "未找到 uv。请先安装 uv。"
has_command npm || fail "未找到 npm。请先安装 Node.js 18+。"

if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  if [ "$AUTO_INSTALL" = "1" ]; then
    info "未发现 frontend/node_modules，正在执行 npm install..."
    (cd "$ROOT_DIR/frontend" && npm install)
  else
    fail "未发现 frontend/node_modules。请先执行 cd frontend && npm install，或用 AUTO_INSTALL=1 ./scripts/start.sh 自动安装。"
  fi
fi

ensure_not_running "后端" "$BACKEND_PID_FILE"
ensure_not_running "前端" "$FRONTEND_PID_FILE"
ensure_port_free "BACKEND" "$BACKEND_PORT"
ensure_port_free "FRONTEND" "$FRONTEND_PORT"

: > "$BACKEND_LOG"
: > "$FRONTEND_LOG"

info "启动后端：http://$BACKEND_HOST:$BACKEND_PORT"
(
  cd "$ROOT_DIR"
  exec uv run python -m uvicorn api.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
) > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
printf '%s\n' "$BACKEND_PID" > "$BACKEND_PID_FILE"

info "启动前端：http://$FRONTEND_HOST:$FRONTEND_PORT"
(
  cd "$ROOT_DIR/frontend"
  exec env VITE_API_URL="$FRONTEND_API_URL" npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --strictPort
) > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!
printf '%s\n' "$FRONTEND_PID" > "$FRONTEND_PID_FILE"

sleep 2
is_running "$BACKEND_PID" || fail "后端启动失败，请查看 $BACKEND_LOG"
is_running "$FRONTEND_PID" || fail "前端启动失败，请查看 $FRONTEND_LOG"

info "项目已启动。前端入口：http://$FRONTEND_HOST:$FRONTEND_PORT"
info "API 地址：http://$BACKEND_HOST:$BACKEND_PORT"
info "日志目录：$LOG_DIR"
info "保持这个脚本运行；按 Ctrl+C 或关闭脚本会退出项目。"

tail -n +1 -F "$BACKEND_LOG" "$FRONTEND_LOG" &
TAIL_PID=$!

while true; do
  if ! is_running "$BACKEND_PID"; then
    info "后端进程已退出，请查看 $BACKEND_LOG"
    exit 1
  fi
  if ! is_running "$FRONTEND_PID"; then
    info "前端进程已退出，请查看 $FRONTEND_LOG"
    exit 1
  fi
  sleep 2
done
