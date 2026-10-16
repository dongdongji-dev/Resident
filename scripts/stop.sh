#!/usr/bin/env bash
# 停止后台 console（及其托管的 kernel 子进程）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/data/logs/console.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "未找到 ${PID_FILE}（console 可能未在后台运行）"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "pid $PID 已不存在，清理 pid 文件"
  rm -f "$PID_FILE"
  exit 0
fi

kill "$PID" 2>/dev/null || true
for _ in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$PID" 2>/dev/null; then
  echo "进程未退出，强制结束 pid $PID"
  kill -9 "$PID" 2>/dev/null || true
fi
rm -f "$PID_FILE"
echo "console (pid $PID) 已停止（kernel 子进程一并结束）"

