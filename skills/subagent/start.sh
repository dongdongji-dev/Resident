#!/usr/bin/env bash
# subagent —— 一键起常驻 hub（v2 规范 / docs/SKILL_V2_SPEC.md）
#
# 通用 skill：这里不写死任何身份/密钥。密钥按下面顺序取：
#   1) 环境变量（已经 export 的优先）
#   2) $SECRETS_DIR（默认 /home/sandbox/.secrets）下的文件：
#        subagent.env（key=value，可多条） / llm_key.txt（纯值或 key=value）
# 数据/状态（pid / port / 日志）一律落 state_dir（默认 /home/sandbox/skill_data/subagent）。
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

# ── state_dir：优先环境变量，其次读配置（local 覆盖 default），最后给默认 ──
STATE_DIR="${SUBAGENT_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$(python3 - "$SKILL_DIR" <<'PY' 2>/dev/null || true
import sys, yaml, pathlib
root = pathlib.Path(sys.argv[1])
for name in ("config/local.yaml", "config/default.yaml"):
    p = root / name
    if p.is_file():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        v = (raw.get("runtime") or {}).get("context_root")
        if v:
            sys.stdout.write(str(v))
            break
PY
)"
fi
STATE_DIR="${STATE_DIR:-/home/sandbox/skill_data/$SKILL_NAME}"
mkdir -p "$STATE_DIR"
PID_FILE="$STATE_DIR/hub.pid"
PORT_FILE="$STATE_DIR/port"
LOG_FILE="$STATE_DIR/hub.log"

# ── 密钥注入 ────────────────────────────────────────────────────────
# .secrets/subagent.env 里的 KEY=VALUE 全部导入（已 export 的不覆盖）
if [ -f "$SECRETS_DIR/subagent.env" ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    case "$key" in
      *[!A-Za-z0-9_]*|'') continue ;;
    esac
    [ -n "${!key:-}" ] && continue
    export "$key=$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//')"
  done < "$SECRETS_DIR/subagent.env"
fi
# 兼容：单独的 llm_key.txt / opencode_go_key.txt 作为 LLM_API_KEY
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
read_secret LLM_API_KEY llm_key.txt || true

if [ -z "${LLM_BASE_URL:-}" ] || [ -z "${LLM_API_KEY:-}" ]; then
  log "⚠️ 缺 LLM_BASE_URL / LLM_API_KEY：设环境变量，或写 $SECRETS_DIR/subagent.env"
  log "   （仍会启动，但 spawn 出来的实例调不动模型）"
fi

# ── 端口 ────────────────────────────────────────────────────────────
PORT="${SUBAGENT_HUB_PORT:-19711}"

# ── 进程控制 ────────────────────────────────────────────────────────
pids() {  # 本 skill 的 hub 进程（按 cmdline + state_dir 精确匹配，避免误杀）
  local pid
  for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    [ -r "/proc/$pid/cmdline" ] || continue
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null \
         | grep -q -- "agent_framework/hub.py" \
       && tr '\0' ' ' < "/proc/$pid/environ" 2>/dev/null \
         | grep -q -- "SUBAGENT_STATE_DIR=$STATE_DIR"; then
      printf '%s\n' "$pid"
    fi
  done
}

stop_all() {
  local pid
  for pid in $(pids); do kill "$pid" 2>/dev/null || true; done
  sleep 1
  for pid in $(pids); do kill -9 "$pid" 2>/dev/null || true; done
}

case "${1:-start}" in
  stop)
    stop_all; rm -f "$PID_FILE"; log "stopped"; exit 0 ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      log "running pid=$(cat "$PID_FILE") port=$(cat "$PORT_FILE" 2>/dev/null) state=$STATE_DIR"
      curl -s -m 3 "http://127.0.0.1:${PORT}/api/status" || true
      log ""
    else
      log "not running (state=$STATE_DIR)"; exit 1
    fi
    exit 0 ;;
  --clean) stop_all ;;
  start) stop_all ;;
  *) log "用法: $0 [start|stop|status|--clean]"; exit 2 ;;
esac

# ── 启动 ────────────────────────────────────────────────────────────
export SUBAGENT_STATE_DIR="$STATE_DIR"
export PYTHONPATH="$SKILL_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
# 清坏代理（外部注入的 127.0.0.1:7890 之类会让内网请求全走代理）
export -n http_proxy https_proxy HTTP_PROXY HTTPS_PROXY || true
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY || true

setsid nohup python3 src/agent_framework/hub.py \
  --config config/default.yaml --config config/local.yaml \
  > "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
# setsid 后 $! 是 setsid 的 pid，真正干活的是子进程 ⇒ 用 pids() 校正
sleep 1
REAL_PID="$(pids | head -1 || true)"
[ -n "$REAL_PID" ] && echo "$REAL_PID" > "$PID_FILE"
echo "$PORT" > "$PORT_FILE"

sleep 3
if curl -s -m 3 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  log "started pid=$(cat "$PID_FILE") port=$PORT log=$LOG_FILE"
else
  log "✗ 未就绪，看日志：$LOG_FILE"
  tail -20 "$LOG_FILE" || true
  exit 1
fi
