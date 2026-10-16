# subagent —— 常驻 subagent hub（通用 skill）

一个常驻 HTTP 服务：注册 / 驱动 / 观察若干「子 agent 实例」。每个实例有独立的
上下文（`context.jsonl`）、独立工作目录和 checkpoint，可以跑长任务，跑完一轮
会主动把结果推给使用者。

> 这是 **通用版**：目录里没有身份、没有密钥、没有机器专属路径。
> 换个人/换台机器 → 改 `config/local.yaml` + 放好自己的 `.secrets/subagent.env` 就能用。

## 目录

```
subagent/
  SKILL.md          # 系统提示词扫的触发说明
  start.sh          # 一键起停：start | stop | status | --clean
  README.md         # 本文件
  config/
    default.yaml    # 通用默认（零身份/零密钥）
    local.yaml      # 本机覆盖（端口、core 地址、模型名、工作目录）
    inputs.yaml     # ★ 输入定义：有哪些输入、哪些唤醒、走哪张表
    prompt/         # 系统提示词片段（identity.md 等）
    skill/          # 给子 agent 用的全局 skill 目录
  src/agent_framework/   # 服务本体（hub.py 是入口）
  actions/               # 对服务的一套操作，一个动作一个目录
  tests/
```

数据（每个实例的 context / session.json / checkpoint）落 `runtime.context_root`，
默认 `skill_data/subagent/`；pid / port / 日志也落那里。

## 起停

```bash
bash start.sh start      # 起（已有实例先杀）
bash start.sh stop       # 停
bash start.sh status     # 看进程 + /api/status
bash start.sh --clean    # 清残留进程
```

密钥从 `.secrets/` 注入（默认 `/home/sandbox/.secrets/subagent.env`）：

```ini
LLM_BASE_URL=http://127.0.0.1:8318/v1
LLM_API_KEY=...
SUBAGENT_LLM_MODEL=<默认模型名>
SUBAGENT_LLM_MODEL_OMEN=<备用模型名>
SUBAGENT_HUB_TOKEN=<hub 鉴权 token，留空=不鉴权>
```

## 输入定义（`config/inputs.yaml`）——这个服务会主动打扰人吗？

三类输入，**代码按声明决定推不推 notice**：

| kind | 含义 | 处理 |
|---|---|---|
| `http` | 外部调 hub 的同步请求（spawn/prompt/approve…） | 直接回响应，不唤醒 |
| `event` | hub 内部产生的异步事件 | 按 `wakeup` / `channel` 决定 |
| `schedule` | 定时自检 | 未启用（留位） |

当前声明（`id` 就是代码里发出的 `kind`）：

| id | 唤醒 | 通道 | 说明 |
|---|---|---|---|
| `completed` | ✅ | notice | 实例跑完一轮且无后续工具调用 |
| `exec_approval` | ✅ | notice | 越界 exec 审批 / ask 提问（阻塞等人处理） |
| `tool_error` | ✅ | notice | 单次工具执行失败（不阻塞） |
| `round_failed` | ✅ | notice | 连续多轮 LLM 失败，已退避 |
| `compressed` | ❌ | none | 上下文被压缩（纯状态） |

**保守默认**：没声明的 kind 一律不推——宁可自己去 `/api/status` 查，也不要被
未知事件吵醒。`dedup_s` 控制同 id 多久内不重复推。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 健康 + 所有实例状态 |
| GET | `/api/profiles` | 可用模型 profile |
| POST | `/api/agents` | 注册实例 `{name, system_prompt?, model_profile?, workdir?}` |
| GET | `/api/agents/<name>/context?limit=N` | 读上下文 |
| POST | `/api/agents/<name>/prompt` | 派任务 `{content}` |
| POST | `/api/agents/<name>/approve` | 审批 `{request_id, approved}` |
| POST | `/api/agents/<name>/ask_reply` | 答复提问 `{ask_id, answers|cancel}` |
| POST | `/api/agents/<name>/compress` | 手动压缩上下文 |
| POST | `/api/agents/<name>/restore` | 回滚 checkpoint `{scope, ref?}` |
| POST | `/api/agents/<name>/switch_model` | 换模型 `{model_profile}` |
| POST | `/api/agents/<name>/close` | 关闭实例 |

`hub.api_key` 非空时，所有 `/api/*` 要求 `X-Api-Token` 头。

## 动作（`actions/`）

一个动作一个目录、一个脚本一件事，一律走 HTTP 调服务（共用 `actions/_hook.py`
找端口/读 token/发请求/统一错误）：

```bash
python3 actions/status/status.py
python3 actions/spawn/spawn.py --name job1 --system-prompt "你是……"
python3 actions/prompt/prompt.py --name job1 --content "做件事"
python3 actions/approve/approve.py --name job1 --request-id <ID> [--reject]
python3 actions/ask_reply/ask_reply.py --name job1 --ask-id <ID> --answers '{"1":"A"}'
python3 actions/context/context.py --name job1 --limit 50
python3 actions/switch_model/switch_model.py --name job1 --profile <profile>
python3 actions/close/close.py --name job1
python3 actions/start/start.py   # = bash start.sh start
```

## 两个已修的坑

1. **LLM 持续报错 → 死循环烧额度**：原来失败后固定 `sleep(0.2)` 重试，后端返回
   持续 400（如订阅过期）时 11 秒能跑 79 轮。现在改成指数退避（0.5→1→2…封顶
   60s）+ 连续 3 次失败发一次 `round_failed` 通知 + 连续 5 次转 300s 冷却。
   参数在 `config/default.yaml` 的 `agent.failure_backoff`。
2. **密钥入库**：`config/.env` 曾被 git 跟踪（里面有 LLM key），已移出索引、
   密钥统一进 `.secrets/`，`.gitignore` 补 `**/.env`、`**/.secrets/`。

## 测试

```bash
PYTHONPATH=src python3 tests/test_inputs_notice.py     # inputs.yaml 是否真的在管 notice
PYTHONPATH=src python3 tests/test_notice_config.py     # notice url/timeout 生效
```
