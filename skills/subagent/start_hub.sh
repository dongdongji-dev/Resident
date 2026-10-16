#!/usr/bin/env bash
# subagent hub 常驻启动脚本（无过期时间，常驻守护）
# 用法: ./start_hub.sh [--config a.yaml [--config b.yaml ...]]
#       不传参数 = 用 v2 分层默认（config/default.yaml + config/local.yaml）
# ⚠️ 2026-10-14 修：原来默认传 `config/config.yaml`（迁移前的单体文件）——
#    用它会**丢掉 local.yaml 里的本机覆盖**（如 gemini profile 的
#    max_tokens/timeout）。改配置后重启别再用裸 CONF。
# 说明: hub 是常驻 HTTP 服务，必须用 setsid 完全脱离会话 + nohup 后台常驻，
#       不能依赖前台同步 exec（会超时被杀、子进程被回收）。
set -e
cd "$(dirname "$0")"

if [ "$#" -gt 0 ]; then
  CONF_ARGS=("$@")
else
  CONF_ARGS=(--config config/default.yaml --config config/local.yaml)
fi
PORT="${SUBAGENT_HUB_PORT:-19711}"
LOG="${SUBAGENT_HUB_LOG:-/tmp/subagent_hub.log}"

# 清坏代理（外部注入的 127.0.0.1:7890 之类会让内网请求全走代理）
export -n http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# 已存在则跳过（幂等）
if curl -s -m 2 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "hub 已在 ${PORT} 运行，跳过启动。"
  exit 0
fi

# 常驻启动：setsid 脱离会话 + nohup 忽略挂断 + 清缓冲
setsid nohup env PYTHONPATH=src python3 src/agent_framework/hub.py "${CONF_ARGS[@]}" \
  > "$LOG" 2>&1 < /dev/null &

PID=$!
echo "hub 已启动 pid=$PID (port=$PORT, log=$LOG, 配置=${CONF_ARGS[*]})"
sleep 2
# 验证
if curl -s -m 3 "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "验证通过：hub 正常响应 ${PORT}"
else
  echo "⚠️ hub 未就绪，最近日志："
  tail -15 "$LOG"
  exit 1
fi