---
name: subagent
description: "Subagent Agent Framework（常驻 HTTP hub 驱动的多 agent 框架，端口 19711）。触发方式：需要运行/管理一个或多个自主 subagent 实例（spawn agent、发 prompt、审批 exec 越界、ask 问答、上下文压缩、checkpoint 回滚、查看架构）时自动调用。核心：start_hub.sh 启动常驻 HTTP hub → POST /api/agents 建实例 → /prompt 驱动 → tool_use 越界审批/ask 交互。架构详见同目录 ARCHITECTURE.md。"
---

# Subagent Agent Framework（使用说明）

一个**常驻 HTTP hub → 多 agent 实例** 的自主 agent 框架。一个 hub 进程占端口
19711，可 spawn 任意多个 subagent 实例（每个实例一个后台线程 + 独立 asyncio
loop + 独立 context 目录），通过 HTTP 端点驱动、审批、交互、压缩、回滚。

本 SKILL.md 是「如何使用 / 如何操作」的手册。内部架构、模块拆分、设计原理、
roadmap 见同目录 **ARCHITECTURE.md**（自动生成，基于真实源码）。

---

## 〇、目录规范

- 配置分层：`config/default.yaml`（通用默认，零身份零密钥）+ `config/local.yaml`
  （本机覆盖，后加载深合并）；旧的 `config/config.yaml` 保留仅作回退参考。
- **输入定义 `config/inputs.yaml`**：三类输入（`http` / `event` / `schedule`）与
  「哪些唤醒我、走哪张表」。**代码已接到「按声明唤醒」**——未声明、`wakeup:false`
  或 `channel:none` 的 event 只落日志、不推 core notice；`dedup_s` 去重。
  当前唤醒的 kind：`completed` / `exec_approval` / `tool_error` / `round_failed`。
- **统一入口 `start.sh`**（`start|stop|status|--clean`，幂等，写入 `skill_data/subagent/`
  的 pid/port/log）；旧的 `start_hub.sh` / `restart_hub.sh` 保留兼容。
- **动作层 `actions/`**：一个动作一个目录（`status`/`spawn`/`prompt`/`approve`/
  `ask_reply`/`context`/`close`/`compress`/`restore`/`switch_model`/`start`/`stop`），
  全部走 HTTP 调常驻服务，共用 `actions/_hook.py`。
- 密钥统一走 `.secrets/`（默认 `/home/sandbox/.secrets/subagent.env`），
  `config/.env` 已废弃并移出 git 索引。
- hub 支持 `hub.api_key` 鉴权（非空时 `/api/*` 要 `X-Api-Token` 头）。
- 详细说明见同目录 **README.md**。

---

## 一、启动 / 重启

```bash
cd /home/sandbox/.skills/subagent
bash start.sh start                  # 常驻启动（幂等）
bash start.sh status                 # 进程 + /api/status
bash start.sh stop                   # 停
python3 actions/status/status.py     # 走动作层看状态
```

- 端口默认 19711（config hub 段，可用 `SUBAGENT_HUB_PORT` 覆盖）。
- **密钥 / LLM 后端**：在 config 里配 `base_url` 与 `api_key`（不进 git）。
  模型名按后端要求写——带厂商前缀还是裸名取决于网关，写错会报 ModelError。
- **context 根**：config 的 `runtime.context_root`（默认 `skill_data/subagent`），
  每个 session 一个子目录。
- 启动脚本内置清坏代理清理（如 127.0.0.1:7890），不依赖外部 env。

---

## 二、HTTP 端点全表

| 端点 | 作用 | 请求体 |
|------|------|--------|
| `GET /api/status` | 所有实例状态（name/state/round/context_path） | - |
| `GET /api/profiles` | 可用 LLM model profile 列表 | - |
| `POST /api/agents` | 注册（spawn）一个新实例 | `{name, system_prompt, workdir}` |
| `GET /api/agents/<name>/context` | 读实例 context.jsonl（分页） | `{start?, end?}` |
| `POST /api/agents/<name>/prompt` | 向实例 push 一条 user prompt | `{content}` |
| `POST /api/agents/<name>/close` | 关闭实例（stop agent+停线程） | - |
| `POST /api/agents/<name>/restore` | checkpoint 回滚 | `{scope: workspace\|conv\|all, ref?}` |
| `POST /api/agents/<name>/switch_model` | 切换模型 | `{model_profile}` |
| `POST /api/agents/<name>/compress` | 手动触发上下文压缩（idle 时） | - |
| `POST /api/agents/<name>/approve` | 审批 exec 越界命令 | `{request_id, approved}` |
| `POST /api/agents/<name>/ask_reply` | 答复 ask 问题 | `{ask_id, answers, cancel?}` |

### spawn 实例示例

```bash
curl -X POST http://127.0.0.1:19711/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"my_agent","system_prompt":"你是…","workdir":"/home/sandbox/proj/x"}'
# → {"ok":true,"agent":{name,state,round,context_path,model_profile}}
```

spawn 后实例即后台常驻（state=idle），随后用 /prompt 唤醒。

### 驱动示例

```bash
curl -X POST http://127.0.0.1:19711/api/agents/my_agent/prompt \
  -H "Content-Type: application/json" -d '{"content":"请完成任务…"}'
# → {"ok":true,"event_id":"..."}  （入队；实例异步处理）
```

实例无工具调用完成时，会经 notice 通道推送「完成任务 + 返回结果」；有工具调用
则进入 TOOL_USE，越界命令触发审批（见下）。

notice 回推端点与超时在 `config/config.yaml` 的 `notice` 段（`url` = core
`/api/notice`、`timeout_s` 默认 6）；改端点或调超时不用改代码。

### ⭐ 喂 prompt 的纪律：只给任务与事实，别做「番茄炒蛋（无东坡肉版）」

给 subagent 的 `content` **只写两样：任务本身 + 它执行需要的事实**。

- ❌ **不注入它没有的上下文**：调用方是谁、你们之间的关系、内部机制、谁要求的、
  为什么这么改。subagent 是一次性工具上下文——这些词它既理解不了、又可能被它当成设定去用。
  ⇒ 要交代参数变动就写客观事实一句：`输出上限已从 32768 改为 100000；客户端超时 600s → 1800s`。
- ❌ **不做"注释式补充"**（类比来自一条公开推文）：
  > 我让它做一盘番茄炒蛋，它往里还加了东坡肉。我说有必要加东坡肉吗？它说你说得对，然后把东坡肉
  > 去掉。我说好，你提 PR 吧。再一看，它 PR 写着「番茄炒蛋（无东坡肉版）」，并且注释里会写一大堆
  > 为什么本道菜不需要加东坡肉。
  ⇒ 要做的事**直接做**：别自己加料；被要求去掉某样东西，也不要把它写进标题再论证一遍"为什么不需要它"。
- ✅ 同理适用于写给别人的任务说明 / 交付说明：**别人要结果，不是推理过程**（除非明确要）。

---

## 三、审批（exec / 文件越界）

安全模型：agent 只能碰自己的工作区（session workdir）。

- **exec 白名单**：`config/config.yaml` exec.whitelist
  （`ls, pwd, cat, python3, python, grep, find, head, tail, wc, mkdir, touch, echo`）。
- 命令名不在白名单，或命令行含越界路径（绝对/相对，URL 除外）→ **触发审批**：
  经 notice 推送审批请求（含 request_id、命令、原因、approve 方式），阻塞等待
  用户 approve/reject（超时按拒绝）。
- 放行方式 `POST /api/agents/<name>/approve`：
  ```bash
  curl -X POST http://127.0.0.1:19711/api/agents/my_agent/approve \
    -H "Content-Type: application/json" \
    -d '{"request_id":"my_agent-xxxx","approved":true}'
  # approved=false 表示拒绝
  ```
- 被拒绝（或超时）→ 工具返回 **403 拒绝码**，不执行；框架不 call，仅写上下文。

文件工具（read/write/edit/replace/append/glob/grep）越界同样触发审批，逻辑一致。

---

## 四、ask 问答（agent 主动问用户）

agent 可经 `ask` 工具向用户提 N 个问题（每题 {question, answers 选项, options 起草}），
阻塞等一次性答复。答复经 `/ask_reply` 提交：

```bash
curl -X POST http://127.0.0.1:19711/api/agents/my_agent/ask_reply \
  -H "Content-Type: application/json" \
  -d '{"ask_id":"my_agent-ask-xxx","answers":{"q1":"A","q2":["B","C"]}}'
# cancel: 传 {"cancel":true} → 返回 403
```

答复格式：`{'q1':'A', 'q2':['B','C',...], 'q3':'自定义串'}`。

---

## 五、agent 可用工具（spawn 后自动注册）

| 工具 | 说明 |
|------|------|
| `echo` | 回显文本 |
| `check_path` | 校验路径是否在工作区内 |
| `exec` | 执行命令（白名单+越界审批） |
| `read` / `write` / `edit` / `replace` / `append` / `glob` / `grep` | 文件工具（越界审批） |
| `ask` | 向用户提问（阻塞等一次性答复） |
| `todo_add` / `todo_list` / `todo_update` | 任务管理（每 session 独立 todo.json） |
| `load_skill` | 加载技能（全局 config/skill + 局部 .skill） |

### todo 用法
```bash
# agent 内调用：
todo_add(task="项目构建", todo="写架构文档")
todo_list()                                   # 列出所有 task/todos（含完成态）
todo_update(task="项目构建", todo="写架构文档", status="completed")
```
存储：`<context_root>/<session>/todo.json`（session 隔离）。

### 额度统计（自动）
每轮 usage 自动累进 session.json 的 `usage_total` 段：
`{cached_input, uncached_input, output, last_usage}`。
- cached_input 每轮累积（上下文缓存前缀，呈二次方增长）
- uncached_input = prompt_tokens - cached_tokens
- output = completion_tokens
查看：`cat <context_root>/<session>/session.json`。

---

## 六、context 与持久化（session 领地）

`<context_root>/<session>/` 下所有产物：

| 文件 | 作用 |
|------|------|
| `context.jsonl` | 认知流唯一真相源（每轮 append / 压缩 write_all 重建） |
| `session.json` | 元数据（workdir、usage_total 额度统计、created_at） |
| `todo.json` | todo 任务清单（session 隔离） |
| `tools.log` | 工具调用审计日志（每次 invoke 一行 JSON） |
| `compressed/` | 压缩归档的旧 context（历史绝不丢） |
| `checkpoints.txt` | checkpoint 索引（时间/标签/work/conv commit） |
| `.git/`、`.workspace-git/` | 双 git 快照（会话 context + 工作区文件，供回滚） |

---

## 七、压缩

- 自动：每轮后 `total_tokens() > max_tokens * threshold` 触发（config compression 段）。
- 手动：`POST /api/agents/<name>/compress`（阻塞上限 `compression.manual_timeout_s`，默认 180s）。
- 只做 step2（上下文压缩），不做 memory 提取；旧 context 归档到 session
  `compressed/`；摘要失败 → 结构化 fallback checkpoint，上下文一定降下来；
  写失败 → 恢复归档（历史绝不丢）。

---

## 八、checkpoint 与回滚

- 每轮/每次 notice 前自动存 checkpoint（双 git：会话 context commit + 工作区 commit）。
- 回滚 `POST /api/agents/<name>/restore`，`scope`：
  - `workspace`：回滚工作区文件
  - `conv`：回滚对话（context）
  - `all`：两者
- `ref` 可指定 commit；缺省回滚最近一次。

---

## 九、常用排障

- hub 没起来：`cat /tmp/subagent_hub.log` 看报错；通常 config/.env 密钥缺失。
- **同步 exec 里跑 `./restart_hub.sh` 会报"执行超时"，但 hub 其实已经起好了**
  （新 hub 常驻、持有调用方管道，executor 等不到管道关闭）。一律用
  `curl -s http://127.0.0.1:19711/api/status` 验，或改用异步 exec。
- agent 卡在某审批：响应 approve 或等超时（approval_timeout_s 默认 300）。
- LOOP 异常/上下文损坏：自动 repair_latest_n 修复尾部；仍异常可 checkpoints 回滚。
- 改代码/配置后：`./restart_hub.sh`。

---

## 十、技能加载（可选进阶）

- 全局技能：`config/skill/<name>/SKILL.md`（所有实例可见）。
- 局部技能：工作区 `.skill/<name>/SKILL.md`（仅对应实例）。
- agent 用 `load_skill` 加载；优先级见 ARCHITECTURE.md §2.8。