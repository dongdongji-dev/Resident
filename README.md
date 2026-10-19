# Resident

**Agent OS Kernel** —— 属于 Agent 的轻量操作系统。内核让一个 AI Agent 常驻运行，Skill 是这台机器的应用程序，命令在容器中隔离执行。

## Resident 是什么

Resident 的目标，是做一套**属于 Agent 的操作系统**：一台常驻的机器，一个 AI Agent 在其中长期生活。对话会压缩、会归档，但工作区里的文件、消息历史、记忆与安排留在机器上，跨重启持续累积。Agent 像使用自己的电脑一样使用它：读写文件、运行程序、安装依赖、启动服务、对外通信；能力不够时，它可以研究、编写并安装新的应用，甚至改造这台机器本身——系统提示词把这套环境直接描述为一台属于 Agent 的机器。

用操作系统的视角描述它：

- **Kernel：尽可能轻量的内核。** 内核只放整个系统缺了就不成立的机制——认知循环、事件队列、心跳调度、上下文持久化与压缩、基础 HTTP 接口。往内核加东西的门槛很高：只有整套系统确实缺少某项能力、且用现有机制无法可靠补上时才考虑；其余的一切都交给应用解决。
- **Skill：应用程序。** 每个技能是一个应用目录：`SKILL.md` 说明它是什么、何时使用、怎么用；目录里可以携带脚本、Actions、常驻服务、配置与测试。装一个技能，就像给这台机器装一个软件。系统提示词常驻技能清单，需要时按名加载全文。
- **Action：操作入口。** 每个 Action 只做一件明确的事，是 Agent 把意图落成操作的接口——作用如同人用鼠标键盘在电脑上完成某个动作；它可以是脚本，也可以是对技能常驻服务的薄封装。
- **Memory：长期记忆。** 记忆文件、消息记录、工作区与归档共同保证连续性——跨对话、跨压缩、跨重启，重要的事情都还在；目前建议的整理方式是类 LLM wiki（Agent 自己维护、持续生长的知识文档）。
- **容器隔离。** 命令与文件操作在 `resident-sandbox` 容器内执行；宿主工作区 `data/workspace` 与容器 `/home/sandbox` 一一对应。

还有一条明确的设计边界：**Resident 为单 Agent 而设计**——一个实例，一个全局 Agent，一条全局上下文。它不擅长多 Agent 协作；多 Agent 场景由 `subagent` 这类技能作为应用自行组织。模型接入保持统一：openai-compatible（generic / deepseek / qwen / glm 等变体）、anthropic、openai responses 三类协议任选；压缩可另配独立模型。

## 能做什么

**常驻与主动性。** 内核进程常驻，Web 控制台负责保活与启停。心跳按秒级节奏运转：检查未读消息、投递到期通知；并默认开启主动唤醒——按概率（未读消息加权）在没有新消息时也唤醒 Agent，可在配置中关闭。是否行动由 Agent 判断，没有值得处理的事情时，静默是正常状态；提示词鼓励它迭代自己：完善工作区、编写工具、安装技能。

**接入聊天平台。** 目前仅适配 QQ：安装脚本可以拉起 NapCat 容器并部署 `qq_adapter` 技能——扫码登录，收发群聊与私聊，按白名单与注意力规则工作。入站消息先落入消息库，由内核按未读与唤醒策略组织给 Agent；出站消息经由适配器的 HTTP 接口。需要其他聊天平台时，可以指引 Agent 参照同样的模式自行开发适配器技能，或使用现成技能。

**沙箱内执行。** 命令与文件操作都在容器内执行，设计上尽量避免阻塞：前台只用于短时间返回的命令（默认超时 10 秒），长任务转异步执行，完成后落盘结果并通知 Agent；文件读写、编辑、搜索在工作区内完成。工作区与宿主目录同源，Agent 可以在其中搭项目、装依赖、跑常驻服务、打磨自己的工具。

**记忆与上下文。** 上下文达到预算后执行两段压缩，两段并行进行：一段把长期有用的内容提取进记忆文件；另一段生成摘要检查点，用摘要与最近若干轮替换原始上下文，让对话继续在预算内进行。

**定时与提醒。** 一次性、延时、Cron（五字段）周期三类通知；到期由心跳转成 Agent 的事件，短提醒与完整正文可以分开设置。

**多模态输入。** 读图工具把图片压缩后带进上下文——直接内联或上传自建图床只留链接；图片事件有字节级准入，避免一次塞入过多撑爆上下文。

**Web 控制台。** 一个简易的浏览器控制平台：聊天流、内核日志、启停与重启、在线编辑配置、安装技能。默认只监听本机，访问需携带 token。

**预装技能。** 默认预装三个技能：`qq_adapter`——QQ 适配器（OneBot11 / NapCat），负责登录、收发消息、白名单与注意力，随 QQ 安装流程部署；`subagent`——独立运行的多 Agent 应用，支持任务派遣、审批、上下文压缩与检查点回滚；`resident_dev`——写给 Agent 的开发文档：内核 HTTP 接口、技能开发规范与记忆组织方式。新增技能只需把目录放进 `data/workspace/.skills/`，或在控制台上传 zip。

**邮箱通道。** 可选启用邮箱（IMAP/SMTP）作为内核内置通道：容器出问题、适配器掉线的场景下，只要内核存活，Agent 依然能收发邮件。

**跨平台部署。** macOS、Linux、Windows 一键安装脚本；docker（全容器）与 host（宿主运行内核、沙箱仍用容器）两种模式；中国大陆网络自动切换国内软件源。

## 如何安装

### 前提

- **Docker**：必需。已安装则确认 `docker info` 正常输出；未安装参考官方文档：<https://docs.docker.com/get-docker/>（macOS / Windows 装 Docker Desktop，Linux 装 Docker Engine）。
- **获取代码**：

```bash
git clone https://github.com/dongdongji-dev/Resident.git
cd Resident
```

手动安装并且选择 host 方式时，另需 Python ≥ 3.10；用一键脚本则不需要提前准备，脚本会检查并在缺失时自动获取。

### 方式一：一键安装脚本

```bash
# macOS / Linux
bash scripts/install.sh

# Windows（PowerShell）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本按顺序询问这些选项，每一项的作用：

1. **部署方式** —— `docker`：控制台、内核、沙箱全部运行在容器里；`host`：控制台与内核用宿主 Python 运行，Docker 只承载沙箱。
2. **模型配置** —— 协议（`openai-compatible` / `anthropic` / `openai(responses)`）、兼容变体（`generic` / `deepseek` / `qwen` / `glm` / `openai`）、服务地址、API Key、模型名，写入 `.env`。
3. **图床（读图）** —— 是否把图片上传图床（需填图床地址与密钥）；不启用则图片以 base64 内联进上下文。
4. **邮箱** —— 是否为 Agent 配置邮箱；启用需填邮箱地址与授权码。
5. **控制台监听** —— 是否对局域网开放；只需本机访问就选否。
6. **鉴权 token** —— 控制台登录密钥与内核密钥；直接回车使用随机生成值。
7. **QQ 适配器（NapCat）** —— 是否安装；安装会拉取容器并暂停等待扫码，随后自动完成接入配置。

镜像走官方源：安装器优先拉取官方镜像（`ghcr.io/dongdongji-dev`），网络不可达或拉取失败时自动改为本地构建（构建走国内源），无需手动选择。

结束后脚本打印访问地址、登录密钥与常用运维命令。

### 方式二：手动安装

**host 方式**（控制台与内核跑在宿主 Python 上）：

```bash
# 1) 配置：模型与密钥
cp .env.example .env
# 编辑 .env：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#            RESIDENT_API_KEY / RESIDENT_CONSOLE_TOKEN（可用 openssl rand -hex 32 生成）

# 2) 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) 构建前端（产物输出到 src/kernel/console/frontend/dist）
cd frontend && npm install && npm run build && cd ..

# 4) 沙箱容器（Agent 命令的执行环境）
docker build --build-arg BASE_IMAGE=python:3.12-slim -t resident-sandbox:latest docker/
mkdir -p data/workspace
# Linux：
docker create --name resident-sandbox --network host \
  -v "$PWD/data/workspace:/home/sandbox" resident-sandbox:latest
# macOS / Windows：
# docker create --name resident-sandbox --add-host host.docker.internal:host-gateway \
#   -v "$PWD/data/workspace:/home/sandbox" -p 19090:19090 resident-sandbox:latest
docker start resident-sandbox

# 5) 启动
bash scripts/start.sh
```

**docker 方式**（全部容器化）：

```bash
cp .env.example .env    # 已含官方镜像来源（ghcr.io）；填模型与密钥
docker compose pull     # 拉官方镜像；网络不可达时跳过此步
docker compose up -d    # 启动（本地无镜像则自动构建）
```

安装完成后打开 <http://127.0.0.1:18082>，用 `.env` 里的 `RESIDENT_CONSOLE_TOKEN` 登录（启动日志里也会打印）。

## 自定义系统提示词

系统提示词按四层拼装：自动生成的身份头（项目名、实际模型、工作目录）、`config/agent.yaml` 中 `prompt.files` 指定的静态提示词文件、已安装技能的清单、当前注册的工具目录。后两层随运行状态实时生成；定义"这个 Agent 是谁"的是前两层。

静态提示词文件默认两个：

- `config/prompt/system/kernel.md` —— 框架级定义：运行理念、行为边界、主动性、记忆与技能的使用约定。属于系统层。
- `config/prompt/user/persona.md` —— 用户自定义人设（示例模板）：名字与自称、对用户的称呼、语气与风格、长期偏好与禁忌、重要关系、长期约定。给 Agent 写性格、人格与个性化定义就是改这里。

修改方式：

- 直接编辑 `config/prompt/user/persona.md`，按模板填写或完全重写；不需要人设时删掉该文件，并同步从 `config/agent.yaml` 的 `prompt.files` 中移除对应行（避免文件缺失告警）。
- 想拆成多个文件（如 `persona.md`、`rules.md`、`knowledge.md`），在 `prompt.files` 里按顺序登记即可。
- 改动**下一次交互即生效**：提示词每次组装都会重新读取这些文件，不需要重启。
- `config/prompt/` 下还集中存放压缩摘要、记忆提取、压缩提醒等环节的提示词，可按同样方式查看与调整。

## 注意事项

- **模型协议**：目前深度测试过的是类 OpenAI 兼容协议（`openai-compatible`）；`anthropic` 与 `openai(responses)` 已经实现，理论上可用，尚未深度测试。
- **执行隔离**：目前只支持 Docker 隔离——沙箱容器是必需组件，两种部署方式都要求安装 Docker；非 Docker 的隔离方式暂不支持，之后可能提供。
- **平台成熟度**：macOS 是主要开发与长期运行环境；Windows 使用独立的 PowerShell 安装脚本，Linux 复用 bash 脚本。Windows 与 Linux 目前完成了平台可用性验证：notice 与 messages 通道经简要测试无明显问题，工具可正常使用；长期运行表现待进一步验证。

## 更新日志

### 2026-10-19

- **专注模式**：Agent 可用 `focus_mode` 工具开启有上限的免打扰窗口——`all` 全屏蔽（周期推送未读统计）/ `whitelist` / `blacklist` 三种模式；被筛除的消息只报销条数、不展开内容；到期或解除后积压立即恢复推送。控制台与聊天页可查看状态、一键解除。
- **QQ 适配器（qq_adapter skill）**：新增 `send_flow` 多发串（串行发送 + 回执核销）；表格渲染升级为论文三线表（五种风格，字体缺失自动回退系统字体）；回执对账支持翻页，修复长落后会话的误报；注意力闲置过期缩短为 120 秒。
- **健壮性**：exec 超时不再丢弃终止前的部分输出；LLM 调用增加流空闲超时（90 秒无数据自动断开重试）；消息工具对「从未出现过的会话名」给出警告提示。
- **系统提示词**：`config/prompt/system/kernel.md` 补充专注模式说明。
