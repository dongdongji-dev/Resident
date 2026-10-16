#!/usr/bin/env bash
# subagent hub 重启脚本（改逻辑后频繁重启用）
# 用法: ./restart_hub.sh [config.yaml]
# 流程: 停旧 hub → 用 start_hub.sh 常驻启动新 hub（无过期时间）
set -e
cd "$(dirname "$0")"

PORT="${SUBAGENT_HUB_PORT:-19711}"

# 停掉旧 hub：匹配实际的 python3 hub 进程（避免匹配到本脚本自身命令行）
# 用 python3 开头前缀，本脚本是 bash，不会误中。
OLD_PIDS=$(pgrep -f "^python3 .*agent_framework/hub\.py" || true)
if [ -n "$OLD_PIDS" ]; then
  echo "停止旧 hub: $OLD_PIDS"
  # 逐个 kill，最多等 10s
  for p in $OLD_PIDS; do
    kill "$p" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    ALIVE=""
    for p in $OLD_PIDS; do
      if kill -0 "$p" 2>/dev/null; then ALIVE=1; break; fi
    done
    [ -z "$ALIVE" ] && break
    sleep 0.5
  done
else
  echo "没有正在运行的 hub"
fi

# 用常驻方式启动新的
echo "启动新 hub..."
exec ./start_hub.sh "$@"