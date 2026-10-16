#!/usr/bin/env bash
# qq_adapter —— 一键起常驻服务
#
# 通用 skill：这里不写死任何身份/密钥。密钥按下面的顺序取：
#   1) 环境变量（已经 export 的优先）
#   2) $SECRETS_DIR（默认 /home/sandbox/.secrets）下的同名文件
#      文件可以是纯值，也可以是 `key=value` / `key: value` 形式（会自动取 value）
# 数据/状态一律落到 state_dir（默认 /home/sandbox/skill_data/qq_adapter）。
#
# 用法：
#   bash start.sh              # 起（已有实例先杀）
#   bash start.sh stop         # 停
#   bash start.sh status       # 看状态
#   bash start.sh --clean      # 强制清残留进程再起
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SKILL_DIR"
SKILL_NAME="$(basename "$SKILL_DIR")"
SECRETS_DIR="${SECRETS_DIR:-/home/sandbox/.secrets}"

log() { printf '%s\n' "$*"; }

# ── state_dir：优先环境变量，其次读配置，最后给默认 ──────────────────
STATE_DIR="${QQ_ADAPTER_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$(python3 - "$SKILL_DIR" <<'PY' 2>/dev/null || true
import sys, yaml, pathlib
root = pathlib.Path(sys.argv[1])
for name in ("config/local.yaml", "config/default.yaml"):
    p = root / name
    if p.is_file():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if raw.get("state_dir"):
            sys.stdout.write(str(raw["state_dir"]))
            break
PY
)"
fi
STATE_DIR="${STATE_DIR:-/home/sandbox/skill_data/$SKILL_NAME}"
mkdir -p "$STATE_DIR"
PID_FILE="$STATE_DIR/adapter.pid"
PORT_FILE="$STATE_DIR/port"
LOG_FILE="$STATE_DIR/adapter.log"

# ── 密钥注入 ────────────────────────────────────────────────────────
read_secret() {  # $1=目标环境变量名  $2=文件名
  local var="$1" file="$SECRETS_DIR/$2"
  [ -n "${!var:-}" ] && return 0
  [ -f "$file" ] || return 1
  local value
  value="$(python3 - "$file" <<'PY'
import re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s*(.+)$", text, re.M)
print((m.group(1) if m else text).strip().strip('"\''))
PY
)"
  [ -n "$value" ] || return 1
  export "$var=$value"
}

read_secret NAPCAT_TOKEN napcat_token.txt || true
read_secret CORE_API_KEY core_api_key.txt || true

if [ -z "${NAPCAT_TOKEN:-}" ]; then
  log "✗ 缺 NAPCAT_TOKEN：设环境变量，或写 $SECRETS_DIR/napcat_token.txt"
  exit 1
fi
if [ -z "${CORE_API_KEY:-}" ]; then
  log "✗ 缺 CORE_API_KEY：设环境变量，或写 $SECRETS_DIR/core_api_key.txt"
  exit 1
fi

# ── 进程控制 ────────────────────────────────────────────────────────
pids() {  # 本 skill 的 adapter 进程（按 state_dir 精确匹配，避免误杀别的实例）
  local pid
  for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    [ -r "/proc/$pid/cmdline" ] || continue
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null \
       | grep -q -- "-m qq_adapter" \
       && tr '\0' ' ' < "/proc/$pid/environ" 2>/dev/null \
       | grep -q -- "QQ_ADAPTER_STATE_DIR=$STATE_DIR"; then
      printf '%s\n' "$pid"
    fi
  done
}

stop_all() {
  local pid
  for pid in $(pids); do
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  for pid in $(pids); do
    kill -9 "$pid" 2>/dev/null || true
  done
}

case "${1:-start}" in
  stop)
    stop_all
    rm -f "$PID_FILE"
    log "stopped"
    exit 0
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      log "running pid=$(cat "$PID_FILE") port=$(cat "$PORT_FILE" 2>/dev/null) state=$STATE_DIR"
    else
      log "not running (state=$STATE_DIR)"
      exit 1
    fi
    exit 0
    ;;
  --clean) stop_all ;;
  start) stop_all ;;
  *) log "用法: $0 [start|stop|status|--clean]"; exit 2 ;;
esac

# ── 启动 ────────────────────────────────────────────────────────────
export QQ_ADAPTER_STATE_DIR="$STATE_DIR"
export PYTHONPATH="$SKILL_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

nohup python3 -m qq_adapter \
  --config config/default.yaml --config config/local.yaml \
  > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 2
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  log "started pid=$(cat "$PID_FILE") port=$(cat "$PORT_FILE" 2>/dev/null) log=$LOG_FILE"
else
  log "✗ 启动失败，看日志：$LOG_FILE"
  tail -20 "$LOG_FILE" || true
  exit 1
fi
