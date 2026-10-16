# 手动部署手册（脚本等价步骤 / 排错补装）

两种用途：

1. 不想用一键脚本，按本文逐步手动完成；
2. 脚本报错后，对照本文定位"哪一步没做完"，手动补上。

> 约定：命令都从**仓库根目录**执行；`$PY` 指 kernel 解释器
> （host 模式 = `.venv/bin/python`；Windows host = `.venv\Scripts\python.exe`）。

## 0. 组件总览

```
浏览器 ── 18082 ──> console ── 18086 ──> kernel ── docker exec ──> resident-sandbox
                      │                     ↑                        （命令/文件操作）
                      │                     └ events_port 18087（事件投递）
                      └ 静态资源 src/kernel/console/frontend/dist + /api 代理
（可选）resident-napcat：WS 3001 / WebUI 6099 ←→ qq_adapter 技能
```

- host 模式：console/kernel 跑在宿主 Python；docker 模式：跑在 compose 的 `resident` 容器内。
- 两种模式下**沙箱都是 Docker 容器**（`resident-sandbox`）。

## 1. 前置检查

```bash
docker info                 # Docker 已装且在运行
python3 -V                  # 仅 host 模式：需要 ≥ 3.10（Windows: py -3 -V）
git clone <仓库地址> && cd Resident   # 或解压发行包
```

## 2. 配置 .env（两种模式都必须）

```bash
cp .env.example .env
```

必需改动（其余保持默认）：

| 变量 | 怎么填 |
|---|---|
| `LLM_BASE_URL` | 模型端点（OpenAI 兼容示例：`https://api.deepseek.com/v1`） |
| `LLM_API_KEY` | 模型密钥 |
| `LLM_MODEL` | 模型名 |
| `RESIDENT_API_KEY` | `openssl rand -hex 32` |
| `RESIDENT_CONSOLE_TOKEN` | `openssl rand -hex 32` |
| `RESIDENT_PYTHON` | 默认 `.venv/bin/python`；仅 **Windows host** 改为 `.venv\Scripts\python.exe` |

注意：**所有变量必须存在**（`config/*.yaml` 里的 `${VAR}` 启动时统一展开，缺一个就起不来）。
`LLM_COMPRESS_*`（独立压缩模型）、`IMGBED_API_KEY`（图床）、`EMAIL_*`（邮箱逃生通道）按需填，
不启用也保留模板值。

模型协议/变体在 `config/agent.yaml`：`llm.protocol` = `openai-compatible` / `anthropic` / `openai`；
`llm.variant` = `generic` / `openai` / `deepseek` / `qwen` / `glm`。

## 3A. host 模式（宿主 Python）

### 3A.1 虚拟环境与依赖

```bash
python3 -m venv .venv                       # Windows: py -3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 含 `-e .`（把仓库装为可编辑包）
```

大陆网络：

```bash
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
```

### 3A.2 构建前端

产物缺失时页面会显示 `frontend not built`。

```bash
cd frontend && npm install && npm run build
# 产物输出到 ../src/kernel/console/frontend/dist/index.html
```

无 Node 的机器用容器构建（**必须挂载整个仓库**——vite 产物在 `frontend/` 之外，
只挂 `frontend/` 会把产物丢在容器里）：

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e npm_config_cache=/tmp/npm-cache \
  -v "$PWD:/build" -w /build node:20-slim \
  bash -c "cd frontend && npm ci --registry=https://registry.npmmirror.com && npm run build"
```

### 3A.3 沙箱镜像与容器

```bash
# 镜像（大陆网络加 --build-arg APT_MIRROR=mirrors.aliyun.com）
docker build --build-arg BASE_IMAGE=python:3.12-slim -t resident-sandbox:latest docker/

# 容器（工作区 data/workspace ↔ 容器 /home/sandbox）
mkdir -p data/workspace
docker create --name resident-sandbox \
  --add-host host.docker.internal:host-gateway \
  -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= \
  -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= \
  -v "$PWD/data/workspace:/home/sandbox" \
  -p 19090:19090 resident-sandbox:latest
docker start resident-sandbox
docker exec resident-sandbox ps aux | head -3   # 验证工具就位
```

（`scripts/start.sh` 会自动做以上全部：镜像缺失则构建、容器缺失则创建。）

### 3A.4 播种技能（可选，安装脚本会做）

```bash
mkdir -p data/workspace/.skills
cp -R skills/resident_dev skills/subagent data/workspace/.skills/   # 已存在则跳过
```

### 3A.5 启动

```bash
bash scripts/start.sh    # 后台启动 + 等待端口就绪 + 打印密钥位置
```

前台调试：

```bash
.venv/bin/python -m kernel.console --config config/console.yaml
# Windows: .venv\Scripts\python.exe -m kernel.console --config config\console.yaml
```

首次启动若无 `.env` 会自动生成一份并打印随机 token。

## 3B. docker 模式（compose）

```bash
docker compose pull           # 官方镜像未发布时会失败——正常，继续走构建
docker compose up -d --build  # 构建并启动 resident + resident-sandbox
docker compose logs -f resident | head -40   # 首次登录密钥打印在启动日志
```

要点：

- `.env` 以文件形式挂载进容器：改完重启容器（`docker compose restart resident`）或前端「重启 Kernel」即生效
- `./data`、`./config` 挂载为 `/app/data`、`/app/config`
- 需要 `docker.sock`（kernel 驱动沙箱）
- 局域网访问：`.env` 里 `RESIDENT_CONSOLE_BIND=0.0.0.0` 后 `docker compose up -d`（端口映射重建）

## 4. NapCat（QQ 适配器，可选）

最省事：`bash scripts/install.sh --with-qq`（自动完成 4.1–4.4，含扫码暂停）。纯手动：

### 4.1 容器与扫码

```bash
mkdir -p data/napcat/config data/napcat/QQ
docker run -d --name resident-napcat --restart unless-stopped \
  -p 3001:3001 -p 6099:6099 \
  -v "$PWD/data/napcat/config:/app/napcat/config" \
  -v "$PWD/data/napcat/QQ:/app/.config/QQ" \
  mlikiowa/napcat-docker:latest

docker logs -f resident-napcat     # 出二维码后手机 QQ 扫码登录
```

### 4.2 开启 WebSocket 服务器（端口 3001）

WebUI `http://127.0.0.1:6099` → 网络配置 → 新增 WebSocket 服务器：
端口 `3001`、Host `0.0.0.0`、消息格式 array、Token 自定义（记住它）。

（安装脚本走等效的文件注入：修改 `data/napcat/config/onebot11_<QQ号>.json`
里 `network.websocketServers`，将容器停-改-启。）

### 4.3 技能与密钥文件

```bash
mkdir -p data/workspace/.skills data/workspace/.secrets
cp -R skills/qq_adapter data/workspace/.skills/          # 若还没有
printf '%s\n' '<你的WS_Token>' > data/workspace/.secrets/napcat_token.txt
grep '^RESIDENT_API_KEY=' .env | cut -d= -f2 > data/workspace/.secrets/core_api_key.txt
chmod 600 data/workspace/.secrets/*.txt

cat > data/workspace/.skills/qq_adapter/config/local.yaml <<'EOF'
napcat:
  ws_url: ws://host.docker.internal:3001
core:
  url: http://host.docker.internal:18086   # docker 模式改为 http://127.0.0.1:18086
EOF
```

### 4.4 验证握手

```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==" -H "Sec-WebSocket-Version: 13" \
  -H "Authorization: Bearer <你的WS_Token>" http://127.0.0.1:3001/
# 期望：HTTP/1.1 101 Switching Protocols
```

## 5. 验证清单

```bash
docker ps | grep resident                             # 容器状态
curl -i http://127.0.0.1:18082/                       # 页面 200；/api/* 无 token 应 401
docker exec resident-sandbox python3 -c "print(1)"    # 沙箱可用
tail -20 data/logs/kernel.log                         # kernel 启动日志
```

## 6. 常见故障与手动修复

| 现象 | 原因 | 修复 |
|---|---|---|
| 页面 `frontend not built` | 前端产物缺失 | 见 3A.2 构建命令 |
| 启动报 `missing required environment variable: X` | `.env` 缺变量（升级后常见 `RESIDENT_PYTHON`） | 补进 `.env` 后重启 |
| 容器内网络全挂 | Docker Desktop 代理注入死代理 | 本项目默认已清空容器代理；仍异常时检查 compose `environment` 与 daemon 代理设置 |
| pip/npm 慢 | 未用国内源 | `.env` 配 `RESIDENT_PIP_INDEX_URL` / `RESIDENT_NPM_REGISTRY` |
| 沙箱镜像构建 apt 慢 | 官方 Debian 源 | 构建加 `--build-arg APT_MIRROR=mirrors.aliyun.com` |
| `docker pull` 卡住 | Docker Hub 不可达 | 给 dockerd / Docker Desktop 配代理（README「中国大陆网络与代理」） |
| 沙箱容器被删/停了 | — | `bash scripts/start.sh` 自动重建；或重做 3A.3 / `docker compose up -d` |
| Windows 中文日志乱码 | 旧进程未重启 | `stop.ps1` + `start.ps1` 或前端「重启 Kernel」 |
| NapCat 扫码后收不到消息 | WS 未开 / Token 不符 / `local.yaml` 连接地址错 | 重做 4.2–4.4；`docker logs resident-napcat` 看报错 |
| 端口冲突 | 18082 / 18086 / 19090 / 3001 被占 | 改 `config/console.yaml`（port、kernel_url）与 `config/agent.yaml`（http.port）；docker 模式同步改 `compose.yml` |

## 7. 卸载与清理

```bash
bash scripts/stop.sh                 # host 模式停止
docker compose down                  # docker 模式停止
docker rm -f resident-napcat         # 如装过 QQ
docker rm -f resident-sandbox
docker rmi resident-sandbox:latest

# 状态与配置全在仓库目录内：data/ 与 .env —— 确要彻底清除时删除即可
```
