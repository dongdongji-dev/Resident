# Subagent Agent Framework 架构文档

> 版本：基于 `subagent` 框架从 0 重建版当前源码（`src/agent_framework/`）。
> 阅读对象：希望理解该框架"为什么这么设计"以及每个模块职责的开发者。
> 说明：本文所有内容均来自对真实源码的逐行阅读，不包含编造。源码中大量注释以
> 关键技术决策与其代码落点，在文末"关键技术决策记录"汇总。

---

## 1. 架构总览图（分层的 ASCII 图）

subagent 框架是一个**常驻 HTTP hub → 多 agent 实例 → 单 Agent ReAct 主循环**的
分层架构。核心设计理念是"事件驱动 + context.jsonl 唯一真相源 + 显式四态状态机"。

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          HTTP 入口层（hub.py）                            │
│   ThreadingHTTPServer :19711                                             │
│   GET /api/status · POST /api/agents （spawn）                           │
│   POST /api/agents/<n>/prompt · close · restore · switch_model ·        │
│        compress · approve · ask_reply                                   │
│   GET /api/agents/<n>/context · /api/profiles                           │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ AgentManager（注册表，持锁）
        ┌───────────────────────┴──────────────────────────┐
        │    每个 agent 一个后台线程 + 独立 asyncio loop       │
        │     (AgentInstance._loop_runner, daemon thread)   │
        ▼                                                    │
┌────────────────────── 核心 Agent（agent.py）──────────────┐  │
│  四态状态机：IDLE / STREAMING / TOOL_USE / COMPRESS        │  │
│                                                           │  │
│  ReAct 主循环 run()：                                      │  │
│  1. IDLE 阻塞 wait_nonempty（不 drain）                    │  │
│  2. →STREAMING sleep 批等待                                │  │
│  3. context.repair_latest 修复尾部                          │  │
│  4. drain 事件 → tool 消息前置、user 合并                   │  │
│  5. 末尾 role=tool 时补临时空 user                          │  │
│  6. _run_round → _run_tools → _maybe_compress → IDLE        │  │
└──────┬──────────┬─────────────┬──────────┬───────────────┘  │
       │          │             │          │                   │
       ▼          ▼             ▼          ▼                   │
┌──────────┐ ┌─────────┐ ┌─────────────┐ ┌─────────────┐      │
│events.py │ │context.py│ │  llm/ 适配器 │ │compression.py│     │
│事件队列   │ │JSONL存储 │ │base/factory │ │上下文压缩      │      │
│Event+Queue│ │唯一真相源 │ │openai-      │ │step2 无memory│      │
│线程安全    │ │+tail修复 │ │compatible/  │ │提取          │      │
└──────────┘ └─────────┘ └─anthropic/   └─────────────┘     │
                           └responses    checkpoint.py ──────┘
                                         双 git 快照/回滚
```

**工具层（被 Agent 的 TOOL_USE 态调用，经 ToolRegistry 路由）**：

```
                ToolRegistry（tools.py，hub 级共享）
                     │  register / schemas / invoke
     ┌───────────────────────────────┬──────────────────────┐
     ▼                               ▼                      ▼
skill_tools.py                    workspace_tools.py       （全局 registry
 load_skill                        check_path / exec        只含 load_skill）
 （异步 handler 保持线程局部）      read/write/delete/
                                   edit/replace/append/
                                   glob/
                                   grep / ask / todo_*
                                   （正式文件/路径工具 + 越界审批，按 session 注册）
```

**身份/提示词层（hub 启动时装配，供系统提示词）**：

```
prompts.py：身份头 → config.prompt.files 提示词文件 → 技能清单 → 工具定义
skills.py ：scan_skills / find_skill / load_skill_content（全局 config/skill + 局部 .skill）
config/   ：config.yaml（一切参数）、prompt/system/identity.md 等提示词、.env 密钥
```

**持久化层（session 领地 = `<context_root>/<session>/`）**：

```
context.jsonl  ← 认知流唯一真相源（Agent 每轮 append / compression write_all 重建）
session.json   ← 元数据（workdir、usage_total 额度统计、created_at）
tools.log      ← 工具调用审计日志（每次 invoke 前一行 JSON）
todo.json      ← todo 任务清单（session 隔离）
compressed/    ← 压缩时归档的旧 context（历史绝不丢）
.workspace-git/← 工作区分离 git（checkpoint.py，快照工作区文件）
.git/          ← 会话 git（跟踪 context.jsonl/session.json/checkpoints.txt）
checkpoints.txt← checkpoint 索引（时间 | label | work_commit | conv_commit）
changes_<tag>.txt ← 变动行数统计（git --numstat）
```

---

## 2. 设计思路与核心原理

### 2.1 为什么用 ReAct 主循环 + 四态状态机

`agent.py` 开头注释明确了单一 Agent 就该是一个**平台无关的 ReAct 循环**。四个
状态各有唯一职责：

- **IDLE**：唯一能消费事件的态。忙时所有入站事件停留在队列里不丢。
- **STREAMING**：LLM 调用在途（或刚从 idle 出来等批窗口）。
- **TOOL_USE**：正在执行工具调用，结果 push 回队列供下一轮。
- **COMPRESS**：上下文压缩运行中。

这个状态机是为了让 Agent 的"空闲等待""思考""调用工具""压缩"能被可观测地
区分（`state` 属性对外暴露，hub 的 status 端点可读），也天然避免了"边等事件边
响应"的竞态——事件只能从 IDLE 进入，忙时自动入队。

### 2.2 context.jsonl 是唯一真相源（单一事实来源）

`context.py` 把所有对话事件/工具结果/系统通知都**顺序追加**到每个 session 的
`context.jsonl`。这带来几个关键保证：

- **重启/换模型可恢复**：Agent 每次构建 LLM 输入都从 `context.load()` 重建，
  不依赖内存态。`switch_model` 停旧建新、复用同一 context 文件即可热切换。
- **可审计**：每一条都带 `event_id/kind/source/created_at`，配合 `tools.log`
  审计，能还原完整认知流。
- **可回滚**：checkpoint 对 context.jsonl 做会话 git，回滚能力建立在它之上。

`agent.py` 特别注意"失败重试去重"：本轮事件先写 context，若 LLM 调用失败会把
同一批事件重新入队，但重试轮按 `event_id` 判断 `has_event_id` 跳过重复写入。

### 2.3 事件队列驱动（只唤醒、不 drain）

`events.py` 的 `EventQueue` 是一个**线程安全的 asyncio 桥接队列**。精髓在
`wait_nonempty()`：它只负责"唤醒"，不 drain。注释明确说：

> 唤醒后 Agent 可先切态/睡眠/修复，再取一个 batch。

这样 Agent 在 `run()` 里可以自由选择时机 drain：
1. idle 等唤醒；
2. 切 STREAMING，**sleep `event_batch_wait_seconds`（0.5s）不碰队列**——让一批
   突发事件一次性落地成一个 batch，减少 LLM 调用次数；
3. 修复 context 尾部；
4. 再 drain 全部。

`push()` 用 `call_soon_threadsafe(self._wake.set)` 保证**任意线程**（HTTP
handler 线程）能安全唤醒 agent loop 线程，无需锁竞争轮询。

### 2.4 事件预处理的三个协议归一化（protocol.py）

把即将发给 LLM 的消息规范化，避免后端拒收：

1. **sanitize_tool_messages**：保证"每个 assistant tool-call 恰好一个紧随的
   role=tool 响应"。缺失的补占位 `"(missing tool response...)"`，孤儿/重复的
   tool 消息丢弃，tool 消息不当首个非 system 消息。
2. **merge_user_events**：批内多条 user 消息合成一条，避免 LLM 收到一堆短
   user 消息。合成时保留第一条的 event_id/kind/source，正文用 `separator` 连接，
   前缀一条 `[系统提示] 批内合并了多条消息` 的 notice。
3. **append_trailing_empty_user**：若发送的消息以 `role=tool` 结尾（部分后端
   拒收），追加一条临时空 user 消息。

### 2.5 approval 审批的安全模型（工具不能越界）

`workspace_tools.py` 是安全模型的核心。机制分两层：

**静态路径校验（check_path）**：
- 每个含 path 参数的工具（exec/read/write/delete/edit/replace/append/glob/grep）
  执行前都先 `_guard_path`。
- `delete` 是**安全删除**（"堵不如疏"）：文件移入回收站
  `workspace_tools.trash_dir`（默认 /home/sandbox/trash/）而非真删，重名自动
  加时间戳后缀（再重名加序号），绝不覆盖；只支持文件，目录拒绝。
- 用 `realpath` 规范化（防 `..`/符号链接/多重 `../../../` 逃逸），判定是否落在
  session 的 `workdir` 工作区内。
- 合法 → 放行；越界无审批通道 → 返回 403 拒绝。

**交互审批（broker）**：
- 越界但有 broker → 调 `broker.request_approval` 发 notice 通知用户，`wait_
  approval` **阻塞等待** `/api/agents/<name>/approve`（`resolve_approval` 用
  `threading.Event` 唤醒）。approve → 放行；reject/超时 → 403。
- exec 另有**命令白名单**（`exec.whitelist`，只匹配命令名=首词）+ 路径扫描双重
  把关，全部合规才直接执行，否则同样走审批。

**为什么用 threading.Event 阻塞**：agent 的工具在 asyncio loop 线程里通过
`asyncio.wait_for` 执行，而审批经 HTTP（hub 线程）注入。用 `threading.Event`
在跨线程间做单向信号，简单可靠地实现了"工具阻塞等用户决策"。

### 2.6 工具结果的"状态码层"（interrupt 语义）

`agent.py` 的 `_read_tool_status` 把工具 handler 返回值归一为 `(code, content)`，
定义了 200/403/408/499/500 五档。设计意图：

- **200 成功** → 结果 push 回事件队列，触发下一轮自动 call；
- **中断类（403 拒绝 / 499 cancel）** → 结果**正常写 context 但不 push 回队列**，
  让 agent 回 IDLE 等外部新输入。这避免模型把"用户拒绝"当成成功继续自主推进
  （防自说自话死循环）；
- **其他异常（408 超时 / 500 失败）** → 可继续 call（结果 push 回队），同时发
  notice 事件通知用户（实例名 + 工具名 + 错误码）。

### 2.7 压缩与 checkpoint：内存/上下文与历史安全的平衡

**compression.py（step2 only）**：
- 自动：每轮完成后 `total_tokens() > max_tokens*threshold` 触发；
- 手动：idle 时经 hub `/compress` 端点触发（force=True 忽略阈值）。
- 流程：归档旧 context（历史不丢）→ 保留最近完整轮次 → 用压缩 LLM 总结整个
  context → 生成 `<conversation-checkpoint>`（summary + recent-context）单条
  user 消息 `write_all` 重建上下文。
- **双保险**：摘要失败重试耗尽 → fallback checkpoint（保证上下文一定降下来）；
  写失败 → 恢复归档旧 context（历史绝不丢）。

**checkpoint.py（双 git）**：
- 每个 session 两套独立 git：**工作区分离 git**（git-dir 在 `.workspace-git/`，
  `--work-tree` 指工作区，不污染工作区自身 `.git`）+ **会话 git**（跟踪
  context.jsonl/session.json/统计文件）。
- `save()` 每轮 notice 前调用：工作区 commit + 会话 commit + 写变动行数统计
  （`git --numstat`）+ 追加 `checkpoints.txt` 索引。
- `restore()` 三档 scope：workspace（只回滚工作区）/ conv（只回滚会话）/
  all（都回滚）。用 `reset --hard`（会删目标 commit 中不存在的文件），ref 或
  label 双解析。

**为什么压缩和 checkpoint 并存**：压缩解决"上下文过长 + 后端拒收"；checkpoint
解决"错误/历史可回滚"。一个向前压缩、一个向后备份，形成对认知流完整性的一对
保护和恢复机制。

### 2.8 技能（skill）加载的解析优先级

`skills.py` + `skill_tools.py` 实现了"全局 + 局部"两级技能：

- **全局**：`config/skill/<name>/SKILL.md`（所有实例共用）；
- **局部**：`<workdir>/.skill/<name>/SKILL.md`（工作区专属）。

`load_skill` 的解析优先级（原设计说明）：
- 全局有局部没有 → 加载全局；
- 只有局部没有全局 → 加载局部；
- 局部全局都有 → 加载 `scope` 参数指定的那个（默认 local）。

**局部根用 `threading.local`** 是因为 ToolRegistry 是 hub 级共享，而局部技能根
因 agent 而异；每个 agent 有独立线程，`_loop_runner` 里 `set_session_skill_root`
后，`load_skill` 用 async handler（coroutine 不切线程）保证在同一 agent loop
线程内可见正确局部根。

### 2.9 todo 与额度统计

- **todo**（`workspace_tools.py`）：每 session 独立的 `todo.json`，参考 resident
  两层结构 `{task: [todo 文案]}`，每个 todo 有完成态。无用户通道依赖，agent 自
  维护任务清单。
- **额度统计**（`agent._accumulate_usage`）：每轮把 usage 的三类 token
  （cached_input / uncached_input / output）累加进 `session.json` 的
  `usage_total` 段，供额度统计/审计。cached_input 来自
  `prompt_tokens_details.cached_tokens`，uncached 是 prompt-cached，output 是
  completion。

---

## 3. 模块逐个拆解

### 3.1 `hub.py` — 常驻 HTTP 入口 + 实例管理（947 行）

**职责**：管理一组 agent 实例，每个实例一个后台 asyncio loop 线程。

关键类/对象：
- **`AgentInstance`**：包装一个 Agent。`start()` 起后台线程；`_loop_runner` 在
  新 loop 里先 `workspace_tools.set_session_state()`（绑定 session/workdir/
  broker 到线程局部）、`skill_tools.set_session_skill_root()`（绑定局部技能根），
  再 `run_until_complete(agent.run())`。提供 `push/stop/status`。
- **`AgentManager`**：注册表 + 决策中枢。核心方法：
  - `_ctx()`：创建/复用 session 目录、`context.jsonl`、写 `session.json`（存
    workdir）；
  - `spawn()`：建 context → `checkpoint.init_repos()` 初始化双 git → `_build_
    agent()` → AgentInstance.start。若有历史上下文则返回 warning 提示"旧 agent"；
  - `_build_agent()`：解析 LLM profile → 创建 tool_logger → `_scan_skills` →
    组装系统提示词 → 构造 Agent；
  - `switch_model()`：stop 实例 → 用新 profile 重建 Agent（复用同一
    context/ctx_path）→ 重新 start；构建失败用原 profile 恢复，保证不丢；
  - `close/prompt/context/restore/compress/status_all`；
  - **审批 broker**：`request_approval/wait_approval/resolve_approval`（exec
    审批）+ `request_ask/wait_ask/resolve_ask`（ask 问答）。用 `threading.Event`
    做跨线程信号，`_exec_approvals` dict + 锁管理。审批/提问都发 notice 通知
    用户，含 approve/ask_reply 的 HTTP 指引；
  - `_default_notice_hook()`：把 notice 转发到 core HTTP `/api/notice`（清代理、
    读 core_api_key；**端点 URL 与请求超时读 config.yaml `notice` 段**
    ——`notice.url` / `notice.timeout_s`，09-16 从硬编码下沉）。
    **转发前每轮先 `checkpoint.save()`**（这才是 checkpoint
    的真正触发点——"每轮触发 notice 前保存"）。
- **`HubHandler`**（BaseHTTPRequestHandler）：GET/POST 路由，`manager` 为类级
  注入。所有端点返回 JSON。
- **`load_config()`**：加载 config.yaml + `.env`，递归展开 `${VAR}`。
- **`main()`**：装配 ToolRegistry（注册 echo/临时文件/load_skill）+ compression
  配置（可选独立压缩 LLM client）+ AgentManager，起 `ThreadingHTTPServer`。

**数据流**：外部 HTTP POST `/prompt` → `manager.prompt` → `inst.push` →
`Agent.push_event` → 事件队列 → agent loop 消费 → 结果经 notice hook 回发用户。

### 3.2 `agent.py` — Agent 主循环 + 工具执行器（502 行）

**职责**：平台无关的 ReAct 循环 + 工具结果状态码层。

关键类/方法：
- `AgentState`（Enum）：IDLE/STREAMING/TOOL_USE/COMPRESS。
- `Agent.__init__`：注入 llm/registry/context/system_prompt + 一堆 config 参数 +
  observer/name/compression/tool_logger。内部建 `EventQueue`。
- `push_event()`：入队 + 日志 + publish `event_queued` 遥测。
- `run()`：主循环。见第 2 节步骤。失败轮把 prepared 事件重新入队 + sleep 0.2s
  自动重试（按 event_id 去重）。
- `_prepare_events()`：tool 消息前置 → non-tool 保持相对顺序 → user 合并。
- `_run_round()`：构建 messages（system prompt + context + incoming）→
  sanitize → append trailing empty user → `llm.stream` → 写 assistant 进
  context → `_accumulate_usage` → 有 tool_calls 则 `_run_tools`，否则发
  `completed` notice。
- `_run_tools()`：`asyncio.gather` 并发调多个工具（`wait_for` 超时后半切），
  `_read_tool_status` 读状态码；按"中断/可继续"分支处理（见 2.6）。
- `_maybe_compress()`/`_run_compress()`：一轮完成后自动判断压缩；手动 force。
- `_accumulate_usage()`：三类 token 累计进 session.json。
- 常量：TOOL_STATUS_OK=200 / REJECTED=403 / TIMEOUT=408 / CANCELLED=499 /
  FAILED=500；INTERRUPT_CODES={403,499}。

**数据流**：事件 → LLM stream（流式 token 经 on_delta 发布）→ assistant 写
context → 工具调用 → tool result 入队/写 context → 循环。

### 3.3 `context.py` — JSONL 上下文存储 + 尾部修复（163 行）

关键类/方法：
- `ContextStore`：`append`（追加一行 JSON）、`write_all`（整文件重写）、`load`
  （逐行解析，坏行跳过）、`has_event_id`（按 event_id 查去重）、`messages_for_
  llm`（把 entry 转成 LLM 消息，带 reasoning/tool_calls/name 等）、`total_tokens`
  （相加所有 assistant usage.total_tokens）、`repair_latest(n)`（修复尾部 N 条）。
- `repair_entry()`：修复损坏的 assistant tool_calls 参数——arguments 非法 JSON
  时试着补 `}` / `"}`，仍不行则替换为结构化 `{"repair_notice":..., "raw_arguments":...}`
  占位，并给 content 追加 `[系统错误]` 说明；非字符串 content 强转 str。
- `repaired/dropped` 返回给 agent 记录日志；修复/丢弃会 rewrite 文件。

### 3.4 `events.py` — 线程安全事件队列（126 行）

- `Event`（dataclass）：role/content/event_id/kind/source/created_at/metadata。
  `from_value`（Event 或 dict 归一）、`as_context_message`（转 context 消息字典）。
- `EventQueue`：deque + Lock；`attach_loop`（绑 asyncio loop + wake event）、
  `push`（跨线程 call_soon_threadsafe 唤醒）、`drain`/`snapshot`/`__len__`、
  `wait_nonempty`（只唤醒不 drain，支持 closed 退出）、`close`。

### 3.5 `tools.py` — 工具注册表 + schema 生成（143 行）

- `schema_from_handler()`：从 handler 签名 + type hints 自动生成 JSON schema
  （Python 类型 → JSON type；Union/NoneType → optional；list → array）。参数无
  默认值 → required。
- `ToolRegister.register()`：可显式给 description/parameters，或自动生成；工具名
  校验 `^[A-Za-z0-9_-]{1,64}$`；重复名会报错。
- `ToolRegistry.invoke()`：async handler 直接 await，同步 handler 用
  `asyncio.to_thread` 转后台线程执行。

### 3.6 `workspace_tools.py` — 正式文件/路径工具 + 越界审批（797 行）

**职责**：check_path + exec + 文件工具（read/write/edit/replace/append/glob/
grep）+ ask + todo_*。

设计原则：变量尽量写 config，不硬编码路径/白名单/限额；工作区根从
session.json 的 workdir 读；exec 白名单/文件工具限额从 config 注入。

关键函数：
- `check_path()`/`check_path_against()`：规范化路径，判断是否在工作区内。
- `_guard_path()`：路径门卫——合法放行；越界无 broker 拒绝；越界有 broker 走
  审批。
- `exec_command()`：串行命令切分 → 命令白名单校验 → 路径扫描 → 全合规直接
  `_run_shell`，否则走 `request_approval` 审批。
- `file_read/write/edit/replace/append/glob/grep`：全部先 `_guard_path`，限额从
  `cfg`（config.yaml workspace_tools 段）读。
- `ask_command()`：JSON 题目数组 → `broker.request_ask` → 阻塞等答复；cancel/
  超时返回 403。
- `todo_add/todo_list/todo_update`：按 session 独立 todo.json。
- `set_session_state()` / `_current()`：线程局部绑定 session/workdir/broker。
- `register_workspace_tools()`：用闭包把 context_root/cfg 注入，注册全部 13 个
  工具（含 check_path）。

### 3.7 `checkpoint.py` — 双 git 快照与回滚（306 行）

已在第 2.7 节详述。要点：`init_repos`（幂等）、`save`（工作区+会话+统计+索引）、
`latest`、`restore`（workspace/conv/all）、`_numstat`/`_linestats_all`（变动行数
统计，用 git 空树常量）、`_resolve_ref`（ref 或 label 双解析）。
常量 `_EMPTY_TREE` = git 空树 `4b825dc642...`。

### 3.8 `compression.py` — 上下文压缩（269 行）

已在第 2.7 节详述。`CompressionConfig`（dataclass）、`load_compression_config`、
`keep_recent_by_usage_diff`（按相邻 assistant usage.prompt_tokens 的差算每轮真实
token 占用）、`summarize`、`build_checkpoint_message`/`build_fallback_checkpoint`
（带 `<conversation-checkpoint>` 包裹）、`_archive`（旧 context 归档到
`compressed/<name>_<ts>.jsonl`）、`should_compress`/`compress_context`。

### 3.9 `skills.py` — 技能扫描与解析（144 行）

`SkillMeta`/`SkillInfo`、`parse_skill_md`（解析 `---frontmatter---` 的 name/
description/trigger）、`skill_files_in`、`scan_skills`（局部优先展示）、
`find_skill`（解析优先级见 2.8）、`load_skill_content`。

### 3.10 `prompts.py` — 系统提示词拼接（117 行）

`build_identity_header`（一句话身份头：项目名 + 真实模型 + 用户工作目录，来自
config 不硬编码）、`load_prompt_files`（按声明顺序加载、缺失跳过告警）、
`build_tools_section`（工具自动定义）、`build_skills_section`（技能清单，区分
全局/局部）、`build_system_prompt`（身份头 → 提示词文件 → 技能清单 → 工具定义）。

### 3.11 `protocol.py` — Provider 协议归一化（99 行）

`sanitize_tool_messages` / `merge_user_events`（含 `make_merged_user`）/
`append_trailing_empty_user`。已在第 2.4 节详述。

### 3.12 `llm/` — 多协议 LLM 适配器

- **`base.py`**：`ToolCall`、`LLMResult`、`DeltaCallback`、`LLMClient`（Protocol，
  统一 `async stream(messages, tools, on_delta)` 契约）、`stream_with_retry`
  （指数退避重试，只重试 retryable 异常，全失败抛 RuntimeError）、`merge_options`。
- **`factory.py`**：`create_llm_client(config)`，按 `protocol` 选实现：
  openai / responses → `OpenAILLM`；anthropic / messages → `AnthropicLLM`；
  openai-compatible / chat-completions / compatible → `OpenAICompatibleLLM`。
- **`openai_compatible.py`**：Chat Completions 后端。`_VARIANTS`（generic/openai/
  deepseek/qwen/glm 各适配 reasoning 字段名），`build_payload` 转聊天消息
  （reasoning 映射到请求键），流式解析 content/thinking/tool_calls 增量。
- **`responses.py`**：OpenAI Responses API。`_response_input` 把消息转
  instructions + input items（function_call / function_call_output / provider_
  items 透传），流式解析各种 `response.*` 事件，保留 `provider_items` 以便加密
  reasoning 后续续接。
- **`anthropic.py`**：Messages API。`_anthropic_messages` 把消息转 system +
  messages（tool_result / tool_use block），流式解析 content_block 事件。
- **`__init__.py`**：导出公共 API。

### 3.13 其它工具模块

- **`skill_tools.py`**：`load_skill` 工具。用 async handler + threading.local
  保持局部技能根在 agent loop 线程可见；`register_skill_tools` 用闭包绑定
  global_root。
- **`logging_setup.py`**：`configure_logging`（级别来自 config）+ `get_logger`；
  INFO=低频有意义事件，DEBUG=高频访问噪声；把 aiohttp 高频 logger 的 INFO 降到
  DEBUG（`_DowngradeHighFreqFilter`）。

---

## 4. 数据流串联（一个完整 prompt 的生命周期）

```
1. curl POST /api/agents  → manager.spawn("demo", workdir=...) 
       ├─ 建 session 目录 + 空 context.jsonl + 写 session.json(workdir)
       ├─ checkpoint.init_repos（工作区分离 git + 会话 git）
       ├─ 扫描 skills → 组装系统提示词
       └─ AgentInstance.start（后台线程 + asyncio loop）→ agent.run() 待命 IDLE

2. curl POST /api/agents/demo/prompt {"content":"...任务..."}
       └─ inst.push → Agent.push_event → 事件入队 + call_soon_threadsafe 唤醒

3. agent loop（IDLE→STREAMING）：
       sleep 0.5s 批等待 → repair_latest → drain → _prepare_events(user 合并) 
       → 写 context → _run_round（sprintf 提示词 + messages_for_llm + incoming）
       → sanitize_tool_messages → llm.stream（流式 token 遥测）

4. LLM 要调工具（如 exec/read）→ agent 态 TOOL_USE：
       _run_tools → ToolRegistry.invoke → workspace_tools handler
       └─ 含 path 参数 → _guard_path：在工作区内直接执行；越界 → broker
          审批（发 notice → 阻塞等 approve / wait_approval）→ 200 或 403
       └─ 结果按状态码分支：成功→push 回队列；中断(403/499)→写 context 不
          入队；异常(408/500)→入队 + notice

5. 无工具调用 → 发 "completed" notice → _default_notice_hook：
       ├─ 先 checkpoint.save（工作区+会话双 commit + 统计 + 索引）
       └─ POST core /api/notice 把结果推给用户（含最新 checkpoint 号）

6. 一轮完成 → _maybe_compress（若 total > max_tokens*threshold）
       └─ compress_context：归档旧 context → 保留近期轮次 → LLM 总结 → 
          <conversation-checkpoint> 单条 user 重建 context

7. 回到 IDLE 等下一个事件。
```

---

## 5. Roadmap

### 5.1 已实现能力清单

| 能力 | 位置 | 状态 |
|------|------|------|
| 常驻 HTTP hub（多 agent 实例管理） | hub.py | ✅ |
| 四态 ReAct 主循环 | agent.py | ✅ |
| context.jsonl 唯一真相源 + 尾部修复 | context.py | ✅ |
| 线程安全事件队列（唤醒-不-drain） | events.py | ✅ |
| 协议归一化（工具消息/user 合并/空尾 user） | protocol.py | ✅ |
| 工具注册表 + schema 自动生成 | tools.py | ✅ |
| 工具结果状态码层（200/403/408/499/500）| agent.py | ✅ |
| 越界审批 / exec 白名单 / ask 问答 | workspace_tools.py + hub.py | ✅ |
| 文件工具（read/write/edit/replace/append/glob/grep） | workspace_tools.py | ✅ |
| todo 任务管理 | workspace_tools.py | ✅ |
| 额度统计（三类 token 累计 session.json） | agent.py | ✅ |
| 上下文压缩（自动+手动，fallback checkpoint） | compression.py | ✅ |
| 双 git checkpoint + 三档回滚 | checkpoint.py | ✅ |
| 全局+局部技能扫描 / load_skill 解析优先级 | skills.py + skill_tools.py | ✅ |
| 多协议 LLM（openai-compatible/anthropic/responses）+ 重试退避 | llm/ | ✅ |
| 系统提示词拼接（身份+提示词文件+技能+工具） | prompts.py | ✅ |
| 工具审计日志（tools.log） | hub.py | ✅ |
| 通知转发 core（含 checkpoint 号） | hub.py _default_notice_hook | ✅ |

### 5.2 已知缺口 / 明确"未实现"

- **无 memory 提取**：compression 只做 step2（上下文压缩），明确**不做**长期
  记忆提取（`memory_extraction.md` 提示词存在但未被压缩流程引用）。注释写
  "参考 resident 压缩逻辑，不做 memory 提取，只做上下文压缩"。
- **无 wake 主动唤醒逻辑**：config 中 wake 段全部注释"未引用，subagent 无 wake
  逻辑"。
- **无 beat 调度**：config beat 段注释"未引用，subagent 无 beat"。
- **无消息/邮箱/数据库适配器**：这些 config 段均注释未引用。
- **无自说自话检测**、未读统计、LLM 失败退避等 resident 特性：均标注未引用。
- **临时工具 `write_file`（file_tools.py）/ `echo`（echo_tools.py）已移除**
  （演示用）：二者仅作 checkpoint 回滚/调用链路演示测试，
  write_file 还会写任意绝对路径无安全约束；正式能力全在 workspace_tools.py。

### 5.3 未来建议路线

1. **补齐 memory 提取**：接入 `memory_extraction.md` 定义的长期归档流程，让压
   缩的早期上下文不只是摘要，还能沉淀进 session 记忆目录。
2. **加 supervisor/健康检查**：目前 hub 无内置 supervisor；长驻进程只能靠
   restart_hub.sh 手动重启。可加 watchdog / systemd unit。
3. **事件队列优先级与背压**：目前单一 FIFO deque；可考虑按 kind/source 分优先
   级、对队列长度做背压/丢弃策略。
4. **审批的会话持久化**：`_exec_approvals` 在内存，hub 重启会丢 pending 审批。
   可考虑落盘或复用 context。
5. **工具 sandbox 强化**：exec 目前 shell=True 直跑；未来可考虑 docker 容器
   隔离（config 里 workspace_tools 容器注释了 resident 的 container 方案）。
6. **完善测试**：现有 selftest_hello.py 是手工自测；可补充单元测试覆盖协议
   归一化、路径逃逸、checkpoint 回滚、压缩 fallback。

---

## 6. 关键技术决策记录

下表汇总关键技术决策与对应代码落点：

| 决策 | 设计意图 | 代码落点 |
|------|---------------------------|----------|
| **工具结果带状态码** | "handler 返回 dict 含 code 即按该字段判定，否则视为成功(200)" | `agent._read_tool_status`；`workspace_tools` 各工具返回 `{"code":...}` |
| **非200不 push、防自推进** | "被拒/超时不 push 回队列，避免模型把被拒当成功继续推进" | `agent._run_tools` 中断类(403/499)只写 context；模块头注释整段说明 |
| **工具审计日志** | 每次 invoke 前记录"用了什么工具/参数/时间" | `agent._run_tools` 的 `tool_logger` 钩子；`hub._make_tool_logger` 写 tools.log |
| **额度统计分三类** | "cached_input 每轮累积呈二次方 / uncached_input / output" | `agent._accumulate_usage` 累加进 session.json usage_total |
| **checkpoint 每轮都在** | "每轮会话 checkpoint 都应在（元数据目录非空）" | `checkpoint.save` 的索引门槛=任一成功即可写；`hub._default_notice_hook` 每轮 notice 前 save |
| **checkpoint 设计** | 每 session 独立双 git，不做跨 agent 共享 | `checkpoint.init_repos/save/restore` + 模块头设计说明 |
| **文件工具限额走 config** | "变量尽量写 config，不硬编码路径/白名单/限额" | `workspace_tools` 从 cfg 读 read/write/edit/glob/grep 限额；config.yaml workspace_tools 段 |
| **notice 回推走 config** | 同上（09-16 下沉：原 URL 与 `timeout=6` 硬编码在 `_default_notice_hook`） | `AgentManager.notice_url/notice_timeout_s` 读 config.yaml `notice` 段；测试 `tests/test_notice_config.py`（14 项，含真发本地服务 + 真超时计时） |
| **压缩归档进 session 领地** | "archive 旧 context 到 skill_data/subagent/<session>/compressed/" | `compression._archive` 归档到 `context.path.parent/compressed`；模块/docstring |
| **压缩只做 step2 无 memory** | "参考 resident 压缩逻辑，不做 memory 提取，只做上下文压缩" | `compression.py` 模块头（step2 only）；无流程引用 memory_extraction.md |
| **临时文件工具已移除** | "仅作 checkpoint 回滚测试用，不考虑安全" | 原 `file_tools.write_file`（任意绝对路径写入）已删，正式文件工具走 workspace_tools |
| **skill 优先级** | "全局有局部没有→全局；只有局部→局部；都有一 scope 指定(默认局部)" | `skills.find_skill` + `skill_tools.load_skill` docstring（逐字复述） |
| **局部技能根线程隔离** | 因 ToolRegistry hub 级共享、局部根因 agent 而异 | `skill_tools` 用 threading.local + async handler；`hub._loop_runner` set_session_skill_root |
| **todo 机制** | "增加 todo 机制……每 session 独立 todo 文件，在 session 领地" | `workspace_tools.todo_*` + `_todo_path`（context_root/session/todo.json）；模块注释 |
| **ask 工具** | 阻塞等一次性答复；cancel/超时→403 本轮 reAct 结束 | `workspace_tools.ask_command` + `hub.request_ask/wait_ask/resolve_ask` |
| **exec 审批** | "whitelist 只填命令名……越界审查……被拒绝→ 403 不执行，框架不 call" | `workspace_tools.exec_command` + `hub.request_approval/wait_approval/resolve_approval`；config.yaml exec 注释 |
| **LLM profile 参数统一** | 真实模型读自所选 profile | `hub._build_agent._profile`、`_assemble_prompt`（不硬编码模型）；模块头注释 |

---

*文末注：本文基于源码逐文件阅读写成，所有行为描述均可在对应模块中找到真实实现。
架构分层图中 `kernel.agent_framework` 经 `src/kernel/agent_framework` 符号链接映射
到 `src/agent_framework/`（`kernel` 是运行期的别名包）。*