#!/usr/bin/env bash
# 后台启动 Resident：先确保 Docker 与沙箱容器就绪，再启动 console
# （kernel 由 console 拉起并保活）。
# 说明：
#   - 监听地址/端口来自 config/console.yaml 的 host/port（默认 127.0.0.1:18082）；
#     kernel 只在 127.0.0.1:<core_port>（默认 18086），经 console 代理不直接对外。
#   - 沙箱：镜像缺失则按 docker/Dockerfile 构建，容器缺失则创建（宿主
#     data/workspace 挂到容器 /home/sandbox），已存在则启动。
#   - 后台运行，日志落 data/logs/console.log；PID 存 data/logs/console.pid。
#   - 所有 /api/* 需 console.api_token：首次启动从 .env.example 生成 .env 并
#     自动生成随机 token（写入 .env，同时打印在启动日志里）。
# 可覆盖环境变量：
#   RESIDENT_CONSOLE_PORT    对外端口（默认 18082，须与 console.yaml 一致）
#   RESIDENT_CONSOLE_CORE    内网 core 端口（默认 18086，须与 console.yaml 一致）
#   RESIDENT_CONSOLE_HOST    监听地址（默认取 console.yaml 的 host，不覆盖）
#   RESIDENT_SANDBOX_IMAGE / RESIDENT_SANDBOX_CONTAINER / RESIDENT_SANDBOX_PORT
# 用法：
#   scripts/start.sh            # 启动
#   scripts/stop.sh             # 停止
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 统一以仓库根为 CWD：console.yaml 里的相对路径（日志等）才对得上
# （否则从其他目录调用 start.sh 会把日志写到 <cwd>/data/logs/ 下）
cd "$ROOT"
PORT="${RESIDENT_CONSOLE_PORT:-18082}"
CORE_PORT="${RESIDENT_CONSOLE_CORE:-18086}"
HOST="${RESIDENT_CONSOLE_HOST:-}"
CONFIG="$ROOT/config/console.yaml"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/data/logs"
LOG="$LOG_DIR/console.log"
PID_FILE="$LOG_DIR/console.pid"

[ -x "$PY" ] || {
  echo "错误：未找到 ${PY}。请先安装（见 README）："
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo "  （前端构建见 README：cd frontend && npm install && npm run build）"
  exit 1
}
[ -f "$CONFIG" ] || { echo "错误：缺少 $CONFIG"; exit 1; }

# ── 1) Docker 与沙箱容器（agent 的命令都在容器里执行）──────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未找到 docker。请先安装 Docker：https://docs.docker.com/engine/install/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker 未运行，尝试启动…"
  case "$(uname -s)" in
    Darwin) open -a Docker ;;
    Linux) (systemctl start docker 2>/dev/null || service docker start 2>/dev/null) || true ;;
  esac
  ok=0
  for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" != "1" ]; then
    echo "错误：Docker 启动超时，请手动启动 Docker 后重试" >&2
    exit 1
  fi
fi
IMAGE="${RESIDENT_SANDBOX_IMAGE:-resident-sandbox}"
CONTAINER="${RESIDENT_SANDBOX_CONTAINER:-resident-sandbox}"
HOST_WORKSPACE="$ROOT/data/workspace"
if ! docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
  echo "沙箱镜像不存在，开始构建 $IMAGE:latest（FROM python:3.12-slim）…"
  docker build \
    --build-arg BASE_IMAGE=python:3.12-slim \
    --build-arg http_proxy= --build-arg https_proxy= \
    --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= \
    -t "$IMAGE:latest" "$ROOT/docker"
fi
mkdir -p "$HOST_WORKSPACE"
if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "沙箱容器不存在，创建 $CONTAINER …"
  docker create --name "$CONTAINER" \
    --add-host host.docker.internal:host-gateway \
    -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= \
    -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= \
    -v "$HOST_WORKSPACE:/home/sandbox" \
    -p "${RESIDENT_SANDBOX_PORT:-19090}:19090" \
    "$IMAGE:latest" >/dev/null
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]; then
  docker start "$CONTAINER" >/dev/null
fi
echo "沙箱容器运行中：${CONTAINER}（$HOST_WORKSPACE -> /home/sandbox）"

# ── 2) console ────────────────────────────────────────────────────────
# 已运行则不重复启动
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "console 已在运行 (pid $(cat "$PID_FILE"))，跳过启动。"
  exit 0
fi

# .env 提示：首次启动会自动生成（含随机 token，见启动日志）
[ -f "$ROOT/.env" ] || echo "提示：未找到 .env —— 首次启动会自动从 .env.example 生成一份，并生成随机鉴权 token（打印在日志里）。" >&2

mkdir -p "$LOG_DIR"
if [ -n "$HOST" ]; then
  nohup "$PY" -m kernel.console --config "$CONFIG" \
    --host "$HOST" --port "$PORT" \
    >> "$LOG" 2>&1 &
else
  nohup "$PY" -m kernel.console --config "$CONFIG" \
    --port "$PORT" \
    >> "$LOG" 2>&1 &
fi
PID=$!
echo "$PID" > "$PID_FILE"

# 等端口就绪（最多 15s）
for _ in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "错误：console 启动失败，日志见 $LOG" >&2
    tail -20 "$LOG" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

echo "console 已后台启动:"
echo "  访问地址:  http://127.0.0.1:$PORT/   （对外监听由 config/console.yaml 的 host 决定）"
echo "  core 端口: 127.0.0.1:${CORE_PORT}（经 console 代理，不直接对外）"
echo "  pid: $PID   日志: $LOG   停止: scripts/stop.sh"
echo "  首次登录密钥见启动日志（tail -20 ${LOG}）或 .env 的 RESIDENT_CONSOLE_TOKEN"
