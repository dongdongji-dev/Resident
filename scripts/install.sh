#!/usr/bin/env bash
# Resident 统一安装脚本（macOS / Linux）。
# Windows 用：powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 设计原则：
#   · 幂等：任何步骤可重复执行（已存在复用/跳过；交互项默认值取当前配置）
#   · 网络自适应：自动探测出口地区（大陆则 pip/npm 用国内源）；
#     Docker 镜像一律官方源（带超时；需代理见安装文档「代理」章节）
#   · 交互确认：需要用户操作的环节（扫码等）会暂停等待
#
# 部署方式（都需要 Docker；agent 命令在沙箱容器内执行）：
#   1) docker 拉取远程镜像运行（未发布/拉取失败时自动本地构建）
#   2) host   宿主 Python（检查/下载 → venv → pip 安装依赖），Docker 承载沙箱与 NapCat
#
# 用法：
#   scripts/install.sh [--mode host|docker] [--with-qq|--no-qq]
#                      [--napcat-config|--napcat-later]
#                      [--cn|--no-cn] [--no-start] [--dry-run] [--yes] [-h]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="" WITH_QQ="" NAPCAT_CFG="" CN=""
ASSUME_YES=0
DRY_RUN=0
NO_START=0
PYTHON=""
UVBIN=""
region=""

# Docker 镜像一律走官方源（带超时；到不了 Docker Hub 时配置代理，见安装文档「代理」章节）。
# pip / npm 等包管理器使用国内源（下方 CN_*）。
CN_PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
CN_NPM_REGISTRY="https://registry.npmmirror.com"
CN_APT_MIRROR="mirrors.aliyun.com"

# ── 基础工具 ─────────────────────────────────────────────────────────
info() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;35m── %s\033[0m\n' "$1"; }
highlight() { printf '\033[1;33m%s\033[0m\n' "$*"; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

gen_token() {
  if has_cmd openssl; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'
  else
    LC_ALL=C tr -dc 'A-Za-z0-9_-' < /dev/urandom | head -c 43 || true
  fi
}

env_val() {  # env_val <文件> <键>
  [ -f "$1" ] || return 0
  awk -F= -v k="$2" '$0 ~ "^"k"=" {sub("^"k"=", ""); print; exit}' "$1"
}

set_env_var() {  # 幂等写入/覆盖 .env 键
  local file="$1" key="$2" value="$3"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ "^"k"=" { print k"="v; done = 1; next }
    { print }
    END { if (!done) print k"="v }
  ' "$file" > "$file.tmp.$$" && mv "$file.tmp.$$" "$file"
}

yaml_top_val() {  # yaml_top_val <文件> <顶层段> <二级键>
  awk -v sect="$2" -v key="$3" '
    /^[A-Za-z_][A-Za-z0-9_]*:/ { line=$0; sub(/:.*/, "", line); cur=line }
    cur == sect && $0 ~ ("^  "key":") {
      sub("^  "key": *", ""); sub(/ *#.*$/, ""); print; exit
    }
  ' "$1"
}

set_yaml_top_key() {  # 幂等设置 agent.yaml 顶层段下的二级键（保留其余内容）
  local file="$1" section="$2" key="$3" value="$4"
  awk -v sect="$section" -v key="$key" -v val="$value" '
    /^[A-Za-z_][A-Za-z0-9_]*:/ { line=$0; sub(/:.*/, "", line); cur=line }
    cur == sect && $0 ~ ("^  "key":") { sub("^  "key":.*", "  "key": "val) }
    { print }
  ' "$file" > "$file.tmp.$$" && mv "$file.tmp.$$" "$file"
}

confirm() {
  local prompt="$1" default="${2:-n}" answer
  if [ "$ASSUME_YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    [ "$default" = "y" ]
    return $?
  fi
  if [ "$default" = "y" ]; then
    read -r -p "$prompt [Y/n] " answer || true
    [ -z "$answer" ] || [ "${answer}" = "y" ] || [ "${answer}" = "Y" ]
  else
    read -r -p "$prompt [y/N] " answer || true
    [ "${answer}" = "y" ] || [ "${answer}" = "Y" ]
  fi
}

ask_value() {  # ask_value <提示> <默认值> [允许空=1]
  local prompt="$1" default="$2" allow_empty="${3:-0}" answer=""
  if [ "$ASSUME_YES" != "1" ] && [ "$DRY_RUN" != "1" ]; then
    read -r -p "${prompt}（当前/默认：${default}）: " answer || true
  fi
  if [ -n "$answer" ]; then printf '%s' "$answer"
  elif [ "$allow_empty" = "1" ]; then printf '%s' ""
  else printf '%s' "$default"; fi
}

pause_user() {  # 醒目提示 + 必须输入 Y 才继续
  local message="$1" answer
  if [ "$ASSUME_YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    return 0
  fi
  while :; do
    printf '\n\033[1;33m[操作] %s\033[0m\n' "$message"
    printf '\033[1;33m完成后输入 Y 继续（Ctrl+C 退出）：\033[0m'
    read -r answer || true
    case "$answer" in
      y|Y|yes|YES|Yes) return 0 ;;
      *) printf '\033[1;33m请输入 Y 确认。\033[0m\n' ;;
    esac
  done
}

wait_port() {
  local port="$1" timeout="$2" i
  [ "$DRY_RUN" = "1" ] && return 0
  for i in $(seq 1 "$timeout"); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# ── 网络出口检测 ─────────────────────────────────────────────────────
detect_region() {
  local out=""
  has_cmd curl || { echo unknown; return; }
  out="$(curl -s --noproxy '*' --max-time 5 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || true)"
  if [ -n "$out" ]; then
    if printf '%s' "$out" | grep -q '^loc=CN$'; then echo cn; else echo global; fi
    return
  fi
  out="$(curl -s --noproxy '*' --max-time 5 https://ipinfo.io/country 2>/dev/null || true)"
  if [ -n "$out" ]; then
    case "$out" in *CN*) echo cn ;; *) echo global ;; esac
    return
  fi
  echo unknown
}

# ── 镜像拉取（官方源 + 硬超时，避免坏网络把安装卡死） ──────────────────
pull_timeout() {  # pull_timeout <超时秒> <镜像>；成功 0；超时 124；失败 1
  local timeout="$1" image="$2" log pid waited=0
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [dry-run] docker pull %s（超时 %ss）\n' "$image" "$timeout"
    return 0
  fi
  log="/tmp/resident-pull.$$.log"
  docker pull "$image" > "$log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$timeout" ]; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null || true
      warn "拉取超时（${timeout}s）：${image}"
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if wait "$pid"; then
    return 0
  fi
  warn "拉取失败：${image}"
  tail -3 "$log" >&2 2>/dev/null || true
  return 1
}

pull_image_fallback() {  # pull_image_fallback <官方名:tag> [超时秒，默认600]
  local official="$1" timeout="${2:-${RESIDENT_OFFICIAL_PULL_TIMEOUT:-600}}"
  if docker image inspect "$official" >/dev/null 2>&1; then
    info "镜像已存在：${official}"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [dry-run] docker pull %s（超时 %ss）\n' "$official" "$timeout"
    return 0
  fi
  info "拉取镜像：${official}"
  pull_timeout "$timeout" "$official" \
    || die "拉取失败：${official}（见安装文档「代理」章节）"
}

# ── 环境检查 ─────────────────────────────────────────────────────────
show_versions() {
  info "本机环境"
  printf '    OS/架构 : %s %s\n' "$(uname -s)" "$(uname -m)"
  if has_cmd docker; then
    printf '    Docker  : %s\n' "$(docker --version 2>/dev/null)"
  else
    printf '    Docker  : 未安装\n'
  fi
  if docker compose version >/dev/null 2>&1; then
    printf '    Compose : %s\n' "$(docker compose version --short 2>/dev/null || echo v2)"
  fi
  if has_cmd python3; then
    printf '    Python  : %s\n' "$(python3 --version 2>&1)"
  else
    printf '    Python  : 未安装\n'
  fi
  if has_cmd node; then
    printf '    Node    : %s\n' "$(node --version 2>/dev/null)"
  fi
}

check_docker() {
  has_cmd docker || die \
    "未找到 docker。请先安装 Docker：https://docs.docker.com/engine/install/ （macOS/Windows 可装 Docker Desktop）"
  docker info >/dev/null 2>&1 || die \
    "Docker 守护进程未运行。请先启动 Docker（macOS：打开 Docker Desktop；Linux：systemctl start docker）"
}

check_compose() {
  docker compose version >/dev/null 2>&1 || die \
    "当前 docker 没有 compose 插件（需要 docker compose v2），请升级 Docker / 安装 compose 插件"
}

find_python() {
  local cand
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if has_cmd "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

ensure_python_via_uv() {  # 用 uv 获取隔离 Python；成功 0（设置 UVBIN/PYTHON）
  if ! has_cmd uv && [ ! -x "$HOME/.local/bin/uv" ]; then
    info "安装 uv（用于获取隔离的 Python 发行包）"
    if [ "$CN" = "1" ]; then
      run bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' || return 1
    else
      run bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' || return 1
    fi
  fi
  UVBIN="uv"
  [ -x "$HOME/.local/bin/uv" ] && UVBIN="$HOME/.local/bin/uv"
  info "获取 Python 3.12"
  run "$UVBIN" python install 3.12 || return 1
  PYTHON="$("$UVBIN" python find 3.12 2>/dev/null || true)"
  [ -n "$PYTHON" ]
}

# ── 配置 .env + agent.yaml ───────────────────────────────────────────
env_bootstrap() {
  local cfg="config/agent.yaml"
  [ -f .env ] || { run cp .env.example .env; info "已从 .env.example 生成 .env"; }
  # kernel 入口固定指向 venv（Docker 与 Unix host 同值；Windows host 由 install.ps1 写入）
  [ -n "$(env_val .env RESIDENT_PYTHON)" ] || run set_env_var .env RESIDENT_PYTHON '.venv/bin/python'
  [ -f "$cfg" ] || die "缺少 ${cfg}"

  # 大陆加速变量（切回非大陆时清空，保持幂等）
  if [ "$CN" = "1" ]; then
    run set_env_var .env RESIDENT_NPM_REGISTRY "$CN_NPM_REGISTRY"
    run set_env_var .env RESIDENT_PIP_INDEX_URL "$CN_PIP_INDEX"
    run set_env_var .env RESIDENT_APT_MIRROR "$CN_APT_MIRROR"
  else
    local old_npm old_pip old_apt
    old_npm="$(env_val .env RESIDENT_NPM_REGISTRY)"
    old_pip="$(env_val .env RESIDENT_PIP_INDEX_URL)"
    old_apt="$(env_val .env RESIDENT_APT_MIRROR)"
    [ -n "$old_npm" ] && run set_env_var .env RESIDENT_NPM_REGISTRY ""
    [ -n "$old_pip" ] && run set_env_var .env RESIDENT_PIP_INDEX_URL ""
    [ -n "$old_apt" ] && run set_env_var .env RESIDENT_APT_MIRROR ""
  fi

  # ── 模型（默认值取当前配置）──
  echo
  echo "  模型配置"
  echo "    协议：1) openai-compatible（最常用）  2) anthropic  3) openai(responses)"
  local cur_proto proto_default proto_choice protocol variant
  cur_proto="$(yaml_top_val "$cfg" llm protocol)"
  case "$cur_proto" in anthropic) proto_default="2" ;; openai) proto_default="3" ;; *) proto_default="1" ;; esac
  proto_choice="$(ask_value '选择协议 1/2/3' "$proto_default")"
  case "$proto_choice" in
    2) protocol="anthropic" ;;
    3) protocol="openai" ;;
    *) protocol="openai-compatible" ;;
  esac
  if [ "$protocol" = "openai-compatible" ]; then
    echo "    variant：1) generic  2) deepseek  3) qwen  4) glm  5) openai"
    local cur_var var_default var_choice
    cur_var="$(yaml_top_val "$cfg" llm variant)"
    case "$cur_var" in
      deepseek) var_default="2" ;; qwen) var_default="3" ;;
      glm) var_default="4" ;; openai) var_default="5" ;; *) var_default="1" ;;
    esac
    var_choice="$(ask_value '选择 variant 1-5' "$var_default")"
    case "$var_choice" in
      2) variant="deepseek" ;; 3) variant="qwen" ;;
      4) variant="glm" ;; 5) variant="openai" ;; *) variant="generic" ;;
    esac
    run set_yaml_top_key "$cfg" llm variant "$variant"
  fi
  run set_yaml_top_key "$cfg" llm protocol "$protocol"
  local base_url api_key model
  base_url="$(ask_value 'LLM_BASE_URL' "$(env_val .env LLM_BASE_URL)")"
  run set_env_var .env LLM_BASE_URL "$base_url"
  api_key="$(ask_value 'LLM_API_KEY ' '（保持当前）')"
  [ "$api_key" = "（保持当前）" ] || run set_env_var .env LLM_API_KEY "$api_key"
  model="$(ask_value 'LLM_MODEL  ' "$(env_val .env LLM_MODEL)")"
  run set_env_var .env LLM_MODEL "$model"
  # 占位值醒目提醒（不阻塞安装，但明确告知系统将不可用）
  case "$(env_val .env LLM_BASE_URL)" in
    ""|*your-llm*|*changeme*)
      highlight "  [注意] LLM_BASE_URL 仍是占位值，安装后无法调用模型（在 .env 填写）"
      ;;
  esac

  # ── 图床（默认值 = 当前是否已启用）──
  echo
  echo "  图床（读图）"
  local imgbed_default="n"
  [ "$(yaml_top_val "$cfg" imgbed enabled)" = "true" ] && imgbed_default="y"
  if confirm "启用图床模式？（读图上传图床，队列只放 URL；否则本地 base64）" "$imgbed_default"; then
    local img_url cur_upload img_key
    cur_upload="$(yaml_top_val "$cfg" imgbed upload_url)"
    img_url="$(ask_value '图床上传地址 upload_url' "$cur_upload")"
    img_key="$(ask_value '图床 API Key' '（保持当前）')"
    [ "$img_key" = "（保持当前）" ] || run set_env_var .env IMGBED_API_KEY "$img_key"
    run set_yaml_top_key "$cfg" imgbed enabled true
    run set_yaml_top_key "$cfg" imgbed upload_url "$img_url"
    run set_yaml_top_key "$cfg" readimage mode imgbed
  else
    run set_yaml_top_key "$cfg" imgbed enabled false
    run set_yaml_top_key "$cfg" readimage mode local
  fi

  # ── 邮箱（默认值 = 当前是否已启用）──
  echo
  echo "  邮箱"
  local email_default="n"
  [ "$(yaml_top_val "$cfg" email enabled)" = "true" ] && email_default="y"
  if confirm "为 agent 设置邮箱？" "$email_default"; then
    local mail_addr mail_pw
    mail_addr="$(ask_value '邮箱地址' "$(yaml_top_val "$cfg" email username)")"
    mail_pw="$(ask_value '邮箱授权码（IMAP/SMTP 同一个）' '（保持当前）')"
    run set_yaml_top_key "$cfg" email enabled true
    run set_yaml_top_key "$cfg" email username "$mail_addr"
    if [ "$mail_pw" != "（保持当前）" ]; then
      run set_env_var .env EMAIL_IMAP_PASSWORD "$mail_pw"
      run set_env_var .env EMAIL_SMTP_PASSWORD "$mail_pw"
    fi
  else
    run set_yaml_top_key "$cfg" email enabled false
  fi

  # ── 控制台监听（是否对局域网开放）──
  echo
  echo "  控制台监听"
  local console_cfg="config/console.yaml"
  [ -f "$console_cfg" ] || die "缺少 ${console_cfg}"
  local lan_default="n"
  if [ "$(yaml_top_val "$console_cfg" console host)" = "0.0.0.0" ] \
     || [ "$(env_val .env RESIDENT_CONSOLE_BIND)" = "0.0.0.0" ]; then
    lan_default="y"
  fi
  if confirm "控制台对局域网开放？（是=0.0.0.0 局域网可访问；否=仅本机 127.0.0.1）" "$lan_default"; then
    run set_yaml_top_key "$console_cfg" console host "0.0.0.0"
    run set_env_var .env RESIDENT_CONSOLE_BIND "0.0.0.0"
    echo "    已开放（/api/* 仍需 token）"
  else
    run set_yaml_top_key "$console_cfg" console host "127.0.0.1"
    run set_env_var .env RESIDENT_CONSOLE_BIND "127.0.0.1"
  fi

  # ── 鉴权 token ──
  echo
  echo "  鉴权 token"
  local key value input
  for key in RESIDENT_API_KEY RESIDENT_CONSOLE_TOKEN; do
    value="$(env_val .env "$key")"
    if [ -z "$value" ] || [ "$value" = "changeme" ]; then
      value="$(gen_token)"
      run set_env_var .env "$key" "$value"
      info "已生成 ${key}"
    else
      input="$(ask_value "${key}" '（保持当前）')"
      [ "$input" = "（保持当前）" ] || run set_env_var .env "$key" "$input"
    fi
  done

  # ── 工作区执行模式（当前版本固定沙箱）──
  if [ "$(yaml_top_val "$cfg" workspace_tools mode)" != "sandbox" ]; then
    warn "workspace_tools.mode 非 sandbox（当前版本仅支持沙箱），已修正"
    run set_yaml_top_key "$cfg" workspace_tools mode sandbox
  fi
}

# ── 工作区与技能 ─────────────────────────────────────────────────────
init_workspace() {
  info "创建工作区 data/workspace"
  run mkdir -p data/workspace/.skills data/workspace/.secrets
  run chmod 700 data/workspace/.secrets
}

seed_skill() {
  local name="$1"
  [ -d "skills/$name" ] || { warn "仓库缺少 skills/${name}，跳过"; return 0; }
  if [ -e "data/workspace/.skills/$name" ]; then
    info "跳过已有 skill：${name}"
  else
    run cp -R "skills/$name" "data/workspace/.skills/$name"
    info "写入 skill：${name}"
  fi
}

ensure_sandbox() {
  local image="${RESIDENT_SANDBOX_IMAGE:-resident-sandbox}"
  local container="${RESIDENT_SANDBOX_CONTAINER:-resident-sandbox}"
  local workspace="$ROOT/data/workspace"
  if ! docker image inspect "${image}:latest" >/dev/null 2>&1; then
    info "构建沙箱镜像 ${image}:latest"
    pull_image_fallback python:3.12-slim
    local apt_mirror
    apt_mirror="$(env_val .env RESIDENT_APT_MIRROR)"
    run docker build --build-arg BASE_IMAGE=python:3.12-slim \
      --build-arg APT_MIRROR="$apt_mirror" \
      --build-arg http_proxy= --build-arg https_proxy= \
      --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= \
      -t "${image}:latest" "$ROOT/docker"
  fi
  run mkdir -p "$workspace"
  if ! docker container inspect "$container" >/dev/null 2>&1; then
    info "创建沙箱容器 ${container}"
    if [ "$(uname -s)" = "Linux" ]; then
      # Linux 引擎的 host.docker.internal 只是路由到 docker 网关
      # （172.17.0.1），打不到绑在宿主回环上的 console/kernel；--network host
      # 后沙箱内 127.0.0.1 即宿主回环（等效 docker 模式的共享网络命名空间）。
      # macOS/Windows 走 Docker Desktop 代拨，桥接网络即可穿透宿主回环。
      run docker create --name "$container" \
        --network host \
        -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= \
        -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= \
        -v "$workspace:/home/sandbox" \
        "${image}:latest"
    else
      run docker create --name "$container" \
        --add-host host.docker.internal:host-gateway \
        -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= \
        -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= \
        -v "$workspace:/home/sandbox" \
        -p "${RESIDENT_SANDBOX_PORT:-19090}:19090" \
        "${image}:latest"
    fi
  elif [ "$(uname -s)" = "Linux" ] \
       && [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container" 2>/dev/null || true)" != "host" ]; then
    warn "沙箱容器是旧网络模式（bridge），沙箱内无法直达宿主 kernel/console。"
    warn "重建生效（工作区数据不受影响）：docker rm -f ${container} 后重跑本脚本"
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo false)" != "true" ]; then
    run docker start "$container"
  fi
  info "沙箱容器运行中：${container}"
}

# ── host 分支安装 ────────────────────────────────────────────────────
host_install() {
  step "安装依赖"
  PYTHON="$(find_python || true)"
  if [ -z "$PYTHON" ]; then
    if confirm "未找到 Python>=3.10。用 uv 自动获取一个隔离的 Python 发行包？" y; then
      ensure_python_via_uv || die \
        "uv 获取 Python 失败。请手动安装 Python 3.10+（brew install python@3.12 / apt install python3.12 / python.org）后重跑"
    else
      die "请安装 Python 3.10+ 后重跑：https://www.python.org/downloads/"
    fi
  fi
  if [ ! -x .venv/bin/python ]; then
    if [ -n "$UVBIN" ]; then
      run "$UVBIN" venv --seed --python 3.12 .venv
    else
      run "$PYTHON" -m venv .venv
    fi
  else
    info "复用 .venv"
  fi
  info "安装 Python 依赖"
  if [ "$CN" = "1" ]; then
    run env PIP_INDEX_URL="$CN_PIP_INDEX" .venv/bin/pip install -r requirements.txt \
      || run .venv/bin/pip install -r requirements.txt
  else
    run .venv/bin/pip install -r requirements.txt
  fi

  step "构建前端"
  if [ -f src/kernel/console/frontend/dist/index.html ]; then
    info "跳过前端构建（产物已存在）"
  elif has_cmd npm; then
    if [ "$CN" = "1" ]; then
      run bash -c "cd frontend && { [ -f package-lock.json ] && npm ci --registry='${CN_NPM_REGISTRY}' || npm install --registry='${CN_NPM_REGISTRY}'; } && npm run build" \
        || run bash -c 'cd frontend && { [ -f package-lock.json ] && npm ci || npm install; } && npm run build'
    else
      run bash -c 'cd frontend && { [ -f package-lock.json ] && npm ci || npm install; } && npm run build'
    fi
  else
    info "无 Node：用 Docker 构建前端"
    pull_image_fallback node:20-slim
    local npm_reg=""
    [ "$CN" = "1" ] && npm_reg="--registry=${CN_NPM_REGISTRY}"
    # 挂载整个仓库根：vite 产物输出到 ../src/kernel/console/frontend/dist，
    # 只挂 frontend/ 会把它丢在容器里；以宿主用户身份运行，避免生成
    # root 属主的 node_modules / dist。
    run docker run --rm --user "$(id -u):$(id -g)" \
      -e HOME=/tmp -e npm_config_cache=/tmp/npm-cache \
      -v "$ROOT:/build" -w /build \
      node:20-slim bash -c "cd frontend && npm ci ${npm_reg} && npm run build"
  fi

  step "准备沙箱"
  ensure_sandbox
}

# ── docker 分支安装 ──────────────────────────────────────────────────
docker_install() {
  step "拉取远程镜像"
  local pulled=0 pid waited=0
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [dry-run] docker compose pull（失败则本地构建）\n'
    pulled=1
  else
    local log="/tmp/resident-compose-pull.$$.log"
    docker compose pull > "$log" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$waited" -ge 300 ]; then
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null || true
        pid=""
        warn "拉取远程镜像超时（300s）"
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
    if [ -n "$pid" ] && wait "$pid"; then
      pulled=1
    fi
  fi
  if [ "$pulled" = "1" ]; then
    info "远程镜像就绪"
    return 0
  fi
  warn "远程镜像不可用，改为本地构建"
  step "预拉基础镜像"
  pull_image_fallback node:20-slim
  pull_image_fallback docker:27-cli
  pull_image_fallback python:3.12-slim
  step "构建镜像"
  if ! run docker compose build; then
    warn "构建失败，排查：docker compose build"
    return 1
  fi
  return 0
}

# ── 启动 ─────────────────────────────────────────────────────────────
startup_attempt() {
  step "启动服务"
  if [ "$MODE" = "docker" ]; then
    if ! run docker compose up -d; then
      warn "启动失败，排查：docker compose logs -f resident"
      return 1
    fi
  else
    if ! run bash "$ROOT/scripts/start.sh"; then
      warn "启动失败，排查：data/logs/console.log"
      return 1
    fi
  fi
  info "等待控制台就绪（18082）…"
  if wait_port 18082 180; then
    info "控制台已就绪：http://127.0.0.1:18082/"
    return 0
  fi
  warn "等待超时，排查："
  if [ "$MODE" = "docker" ]; then
    warn "  docker compose logs -f resident"
  else
    warn "  tail -50 data/logs/console.log"
  fi
  return 1
}

# ── NapCat（QQ 适配器） ──────────────────────────────────────────────
sandbox_running() {
  [ "$(docker inspect -f '{{.State.Running}}' resident-sandbox 2>/dev/null || echo false)" = "true" ]
}

napcat_install() {
  step "安装 NapCat"
  seed_skill qq_adapter
  pull_image_fallback mlikiowa/napcat-docker:latest 1800
  run mkdir -p data/napcat/config data/napcat/QQ
  if ! docker container inspect resident-napcat >/dev/null 2>&1; then
    info "创建 NapCat 容器（WS 3001 / WebUI 6099）"
    run docker create --name resident-napcat \
      -p 3001:3001 -p 6099:6099 \
      -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= \
      -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= \
      -v "$ROOT/data/napcat/config:/app/napcat/config" \
      -v "$ROOT/data/napcat/QQ:/app/.config/QQ" \
      --restart unless-stopped \
      mlikiowa/napcat-docker:latest
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' resident-napcat 2>/dev/null || echo false)" != "true" ]; then
    run docker start resident-napcat
  fi
  info "NapCat 容器运行中"
}

write_installer_helpers() {
  run mkdir -p data/workspace/.installer
  run bash -c 'cat > data/workspace/.installer/ws_inject.py' <<'PYEOF'
"""把 OneBot11 WebSocket 服务器（端口 3001）注入 NapCat 配置（幂等）。
用法：python3 ws_inject.py <napcat配置目录>；token 走环境变量 WS_TOKEN。"""
import json, os, sys, glob
cfg_dir = sys.argv[1]
token = os.environ.get("WS_TOKEN", "")
files = sorted(glob.glob(os.path.join(cfg_dir, "onebot11_*.json")),
               key=os.path.getmtime, reverse=True)
if not files:
    print("NO_CONFIG"); sys.exit(1)
path = files[0]
qq = os.path.basename(path)[len("onebot11_"):-len(".json")]
doc = json.load(open(path, encoding="utf-8"))
net = doc.setdefault("network", {})
servers = net.setdefault("websocketServers", [])
entry = next((e for e in servers if isinstance(e, dict) and e.get("port") == 3001), None)
if entry is None:
    entry = {"enable": True, "name": "resident", "host": "0.0.0.0", "port": 3001,
             "reportSelfMessage": True, "enableForcePushEvent": True,
             "messagePostFormat": "array", "token": token, "debug": False,
             "heartInterval": 30000}
    servers.append(entry)
elif token:
    entry["token"] = token
json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"OK {os.path.basename(path)} qq={qq} token={'set' if entry.get('token') else 'empty'}")
PYEOF
  run bash -c 'cat > data/workspace/.installer/ws_probe.py' <<'PYEOF'
"""从沙箱内做一次裸 WebSocket 握手，验证 NapCat WS（url+token）可用。
环境变量：WS_URL（默认 host.docker.internal:3001）、WS_TOKEN。
退出码：0 = 101 成功；1 = 失败。"""
import base64, os, socket, sys
url = os.environ.get("WS_URL", "host.docker.internal:3001")
token = os.environ.get("WS_TOKEN", "")
host, _, port = url.partition(":")
port = int(port or 3001)
try:
    s = socket.create_connection((host, port), timeout=6)
except OSError as exc:
    print(f"CONNECT-FAIL {exc}"); sys.exit(1)
try:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
           f"Authorization: Bearer {token}\r\n\r\n")
    s.sendall(req.encode())
    head = s.recv(4096).decode("utf-8", "replace").splitlines()
    first = head[0] if head else ""
    print(f"HTTP {first}")
    sys.exit(0 if " 101 " in first else 1)
finally:
    s.close()
PYEOF
}

napcat_configure() {  # 成功 0；失败 1
  step "配置 NapCat"
  write_installer_helpers
  echo
  echo "  QQ 登录："
  echo "    1. 查看二维码：docker logs -f resident-napcat（或 WebUI：http://127.0.0.1:6099）"
  echo "    2. 手机 QQ 扫码登录"
  echo
  pause_user "完成 QQ 扫码登录"
  if [ "$DRY_RUN" = "1" ]; then
    printf '    [dry-run] 轮询 onebot11_*.json → 停容器 → 注入 WS(3001) → 启容器\n'
    printf '    [dry-run] 写 .secrets/napcat_token.txt、core_api_key.txt、local.yaml\n'
    printf '    [dry-run] docker exec 沙箱 ws_probe.py（存活尝试）\n'
    return 0
  fi
  info "检测登录配置…"
  local waited=0 found=""
  while [ "$waited" -lt 120 ]; do
    found="$(ls data/napcat/config/onebot11_*.json 2>/dev/null | head -1 || true)"
    [ -n "$found" ] && break
    sleep 2
    waited=$((waited + 2))
  done
  if [ -z "$found" ]; then
    warn "未检测到登录配置（onebot11_*.json）"
    warn "确认扫码成功后重跑：scripts/install.sh --with-qq"
    return 1
  fi
  info "检测到配置：$(basename "$found")"
  local token existing_token
  existing_token="$(awk -F'"' '/"port": *3001/{p=1} p&&/"token"/{print $4; exit}' "$found" 2>/dev/null || true)"
  token="$(ask_value '设置 WebSocket Token（留空自动生成）' "${existing_token:-（自动生成）}" 1)"
  if [ -z "$token" ] || [ "$token" = "（自动生成）" ]; then
    if [ -n "$existing_token" ]; then token="$existing_token"; else token="$(gen_token)"; fi
  fi
  # docker 模式下先启动服务：存活检查要沙箱容器在跑（幂等；最后的启动步会是空操作）
  if [ "$MODE" = "docker" ] && ! sandbox_running; then
    info "启动服务进行沙箱存活检查"
    if ! run docker compose up -d; then
      warn "启动失败，无法存活检查。重跑：scripts/install.sh --with-qq"
      return 1
    fi
    local j
    for j in $(seq 1 60); do
      sandbox_running && break
      sleep 1
    done
  fi
  info "停止 NapCat，写入 WS 配置"
  run docker stop resident-napcat >/dev/null
  info "注入 WebSocket 服务器（端口 3001）"
  run docker run --rm \
    -v "$ROOT/data/workspace/.installer:/inst:ro" \
    -v "$ROOT/data/napcat/config:/cfg" \
    -e "WS_TOKEN=${token}" \
    --entrypoint python3 resident-sandbox:latest /inst/ws_inject.py /cfg \
    || { warn "WS 注入失败（检查 NapCat 配置目录）"; return 1; }
  run docker start resident-napcat >/dev/null
  info "等待 WS 端口 3001…"
  local i
  for i in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/127.0.0.1/3001") 2>/dev/null; then break; fi
    sleep 1
  done
  run mkdir -p data/workspace/.secrets
  printf '%s\n' "$token" > data/workspace/.secrets/napcat_token.txt
  chmod 600 data/workspace/.secrets/napcat_token.txt
  env_val .env RESIDENT_API_KEY > data/workspace/.secrets/core_api_key.txt
  chmod 600 data/workspace/.secrets/core_api_key.txt
  local core_url ws_url
  if [ "$MODE" = "docker" ]; then
    core_url="http://127.0.0.1:18086"; ws_url="ws://host.docker.internal:3001"
  elif [ "$(uname -s)" = "Linux" ]; then
    core_url="http://127.0.0.1:18086"; ws_url="ws://127.0.0.1:3001"
  else
    core_url="http://host.docker.internal:18086"; ws_url="ws://host.docker.internal:3001"
  fi
  cat > data/workspace/.skills/qq_adapter/config/local.yaml <<EOF
# 本机覆盖（由 scripts/install.sh 生成；通用默认见 config/default.yaml）
napcat:
  ws_url: ${ws_url}
core:
  url: ${core_url}
EOF
  if sandbox_running; then
    info "沙箱内存活检查"
    local probe_ws="host.docker.internal:3001"
    if [ "$MODE" = "host" ] && [ "$(uname -s)" = "Linux" ]; then probe_ws="127.0.0.1:3001"; fi
    if docker exec -e "WS_URL=${probe_ws}" -e "WS_TOKEN=${token}" \
        resident-sandbox python3 /home/sandbox/.installer/ws_probe.py; then
      info "NapCat WS 存活检查通过"
      return 0
    fi
    warn "存活检查失败（token 或网络）。重跑：scripts/install.sh --with-qq"
    return 1
  fi
  warn "沙箱未运行，跳过存活检查（重跑 --with-qq 补检）"
  return 1
}

# ── 收尾输出 ─────────────────────────────────────────────────────────
print_summary() {
  local cfg="config/agent.yaml"
  local llm_url llm_key llm_model llm_placeholder=0
  llm_url="$(env_val .env LLM_BASE_URL)"
  llm_key="$(env_val .env LLM_API_KEY)"
  llm_model="$(env_val .env LLM_MODEL)"
  case "$llm_url" in ""|*your-llm*|*changeme*) llm_placeholder=1 ;; esac
  [ "$llm_key" = "changeme" ] && llm_placeholder=1
  case "$llm_model" in ""|*your-model*|*changeme*) llm_placeholder=1 ;; esac
  echo
  if [ "$NO_START" = "1" ]; then
    info "安装完成（未启动）"
  else
    info "安装完成"
  fi
  echo "  ─────────────────────────────────────────────"
  echo "  访问地址：http://127.0.0.1:18082/"
  printf '  登录密钥 RESIDENT_CONSOLE_TOKEN = %s\n' "$(env_val .env RESIDENT_CONSOLE_TOKEN)"
  printf '  内部密钥 RESIDENT_API_KEY       = %s\n' "$(env_val .env RESIDENT_API_KEY)"
  printf '  模型：protocol=%s variant=%s model=%s\n' \
    "$(yaml_top_val "$cfg" llm protocol)" \
    "$(yaml_top_val "$cfg" llm variant)" \
    "$(env_val .env LLM_MODEL)"
  printf '  LLM_BASE_URL = %s\n' "$(env_val .env LLM_BASE_URL)"
  if [ -f data/workspace/.secrets/napcat_token.txt ]; then
    printf '  NapCat WS Token = %s\n' "$(cat data/workspace/.secrets/napcat_token.txt)"
    echo '  NapCat WS：宿主 ws://127.0.0.1:3001 / 沙箱 ws://host.docker.internal:3001'
    echo '  NapCat WebUI：http://127.0.0.1:6099'
  fi
  if [ "$llm_placeholder" = "1" ]; then
    echo "  ─────────────────────────────────────────────"
    highlight "  [注意] LLM 配置仍是占位值，系统无法调用模型"
    highlight "     编辑 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启："
    if [ "$MODE" = "docker" ]; then
      highlight "     前端「重启 Kernel」按钮（或 docker compose up -d）"
    else
      highlight "     前端「重启 Kernel」按钮（或 scripts/start.sh）"
    fi
  fi
  echo "  ─────────────────────────────────────────────"
  echo "  运维："
  if [ "$MODE" = "docker" ]; then
    echo "    启动/停止：docker compose up -d / docker compose stop"
    echo "    日志：docker compose logs -f resident"
  else
    echo "    启动/停止：scripts/start.sh / scripts/stop.sh"
    echo "    日志：tail -f data/logs/console.log"
  fi
  echo "    改模型：.env 的 LLM_*，改后重启"
  echo "    改监听：config/console.yaml console.host（host）"
  echo "            .env RESIDENT_CONSOLE_BIND（docker）"
  echo "    改图床：config/agent.yaml imgbed + .env IMGBED_API_KEY；readimage.mode"
  echo "    改邮箱：config/agent.yaml email + .env EMAIL_IMAP/SMTP_PASSWORD"
  echo "    改人设：config/prompt/user/persona.md（自定义系统提示词，下一轮生效）"
  echo "    加技能：目录拷入 data/workspace/.skills/"
}

# ── 参数 ─────────────────────────────────────────────────────────────
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode) shift; MODE="${1:-}" ;;
    --mode=*) MODE="${1#--mode=}" ;;
    --with-qq) WITH_QQ="1" ;;
    --no-qq) WITH_QQ="0" ;;
    --napcat-config) NAPCAT_CFG="1" ;;
    --napcat-later) NAPCAT_CFG="0" ;;
    --cn) CN="1" ;;
    --no-cn) CN="0" ;;
    --no-start) NO_START=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
  shift
done
[ "$DRY_RUN" = "1" ] && ASSUME_YES=1
case "$MODE" in ""|host|docker) ;; *) die "--mode 只能是 host 或 docker" ;; esac

# ── 主流程 ───────────────────────────────────────────────────────────
echo "Resident 安装"

step "环境"
show_versions

step "网络检测"
if [ -z "$CN" ]; then
  region="$(detect_region || true)"
  case "$region" in
    cn) info "检测到大陆网络出口"; CN="1" ;;
    global) info "检测到非大陆网络出口"; CN="0" ;;
    *)
      warn "出口检测失败（无网络？）"
      if [ "$ASSUME_YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then CN="0"
      else confirm "按大陆网络处理？" n && CN="1" || CN="0"; fi
      ;;
  esac
fi
if [ "$CN" = "1" ]; then info "网络判定：大陆（pip/npm 使用国内源）"; else info "网络判定：非大陆"; fi

step "前置检查"
check_docker
if [ -z "$MODE" ]; then
  if [ "$ASSUME_YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    MODE="docker"
  else
    echo "  部署方式："
    echo "    1) docker 拉取远程镜像运行（未发布时自动本地构建）"
    echo "    2) host   宿主 Python（venv + pip），Docker 承载沙箱"
    read -r -p "  选择 [1/2，默认 1]: " mode_choice || true
    case "$mode_choice" in 2|host) MODE="host" ;; *) MODE="docker" ;; esac
  fi
fi
[ "$MODE" = "docker" ] && check_compose
info "部署方式：${MODE}"

info "镜像拉取：官方源"

step "配置"
env_bootstrap

step "工作区与技能"
init_workspace
seed_skill resident_dev
seed_skill subagent

# subagent notice 回推地址：macOS host 模式的沙箱是桥接容器，127.0.0.1
# 打不到宿主 kernel，写 local.yaml 覆盖为 host.docker.internal；Linux host
# （沙箱 --network host）与 docker 模式默认 127.0.0.1 即可，不写。
if [ "$MODE" = "host" ] && [ "$(uname -s)" = "Darwin" ] \
   && [ ! -f data/workspace/.skills/subagent/config/local.yaml ]; then
  run mkdir -p data/workspace/.skills/subagent/config
  run bash -c 'cat > data/workspace/.skills/subagent/config/local.yaml' <<'EOF'
# 本机覆盖（由安装脚本生成；通用默认见 config/default.yaml）
notice:
  url: http://host.docker.internal:18086/api/notice
EOF
  info "已为 subagent 写入 local.yaml（host.docker.internal）"
fi

if [ "$MODE" = "docker" ]; then
  if ! docker_install; then die "安装失败：镜像构建未通过"; fi
else
  host_install
fi

# NapCat
napcat_configured=0
napcat_ok=1
if [ -z "$WITH_QQ" ]; then
  napcat_default="n"
  [ -e data/workspace/.skills/qq_adapter ] && napcat_default="y"
  if confirm "是否安装 NapCat（QQ 适配器，含容器）？" "$napcat_default"; then WITH_QQ="1"; else WITH_QQ="0"; fi
fi
if [ "$WITH_QQ" = "1" ]; then
  napcat_install
  if [ -z "$NAPCAT_CFG" ]; then
    cfg_default="n"
    ls data/napcat/config/onebot11_*.json >/dev/null 2>&1 && cfg_default="y"
    if [ "$ASSUME_YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then
      [ "$cfg_default" = "y" ] && NAPCAT_CFG="1" || NAPCAT_CFG="0"
    else
      if confirm "现在配置 NapCat（扫码登录 + 自动建 WS）？" "y"; then NAPCAT_CFG="1"; else NAPCAT_CFG="0"; fi
    fi
  fi
  if [ "$NAPCAT_CFG" = "1" ]; then
    if napcat_configure; then napcat_configured="1"; else napcat_ok=0; fi
  else
    info "NapCat 未配置，之后重跑：scripts/install.sh --with-qq"
  fi
fi

if [ "$napcat_ok" != "1" ]; then
  die "安装失败：NapCat 未配置完成。重跑：scripts/install.sh --with-qq"
fi

if [ "$NO_START" = "1" ]; then
  step "跳过启动"
  echo "    启动：scripts/start.sh（host） / docker compose up -d（docker）"
  print_summary
  exit 0
fi

if startup_attempt; then
  print_summary
  exit 0
fi
die "安装失败：服务启动未通过（见上方命令）"
