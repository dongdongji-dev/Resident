# core / kernel HTTP 接口（Resident Kernel）

> 面向在容器内自建程序/服务的开发者。**接口口径以本文为准**；`docs/DEVELOPMENT.md` 是更早的版本，
> 与其冲突处听本文（差异见文末「与旧文档的差异」）。

## 1. 端口与认证

| 服务 | 地址 | 用途 |
|---|---|---|
| core（kernel） | `http://<宿主>:18086` | Agent 本体：状态/上下文/消息库/通知 |
| events_port | `http://<宿主>:18087` | 投递「未定义事件」给 kernel（当前仅入队记录） |
| console | `http://<宿主>:18082` | 前端/管理面（非 Agent 主要使用） |

- `<宿主>` 按部署方式取：
  - **docker 模式、Linux host 模式** → `127.0.0.1`
  - **macOS / Windows host 模式** → `host.docker.internal`
- 拿不准就**先试 `127.0.0.1:<端口>`，不通再换 `host.docker.internal:<端口>`**。
- 认证：请求头 `X-Api-Token: <key>` 或 `Authorization: Bearer <key>`；**SSE 只能 `?token=<key>`**。
- key 源：`/home/sandbox/.secrets/core_api_key.txt`（`core_api_key=` 那行的右值，权限 600）。
  ⚠️ 文件前几行是 `#` 注释，解析要跳注释取含 `=` 行；沙箱 `HOME=/root`，路径一律写死 `/home/sandbox`。
- 失败返回 `401 unauthorized` ＝ key 不对。

## 2. 端点（已验证可用）

### 状态与上下文
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活（ok/state/alive） |
| GET | `/api/state` | 状态 + 轮次 + 工具列表 + 队列长度 |
| GET | `/api/queue` | 事件队列快照 |
| GET | `/api/context?n=N` | 上下文（最近 N 条） |

### 消息域
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/messages` | **渠道入站正式入口**。body：`{platform, conversation_id, remote_message_id, sender_data?, content, message_type?, direction?, wakeup?}`；`conversation_key = platform:conversation_id:remote_message_id` 自动去重，返回 `201` 或 `deduped: true`；落库为未读，beat 会唤醒 agent |
| GET | `/api/messages/unread?limit=` | 未读列表（出站仲裁用，支持过滤） |
| GET | `/api/messages?platform=&conversation_id=&after_seq=&limit=&direction=` | 会话内消息（seq 升序） |
| GET | `/api/messages/archive?…` | 归档消息查询 |
| POST | `/api/messages/archive` | 归档 N 天前**已读**消息（未读永不归档） |

### 通知域（notice）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/notice` | 创建通知。body：`{content, kind?("once"\|"cron"), due_at?, delay_seconds?, cron?, notification?}` → `{id}`，到期由 beat 推送 |
| GET | `/api/notice?include_done=1` / `GET /api/notice/{id}` / `DELETE /api/notice/{id}` | 通知管理 |

### 专注模式域（focus）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/focus` | 查询专注状态：`{active, mode("all"\|"whitelist"\|"blacklist"), sessions[], until, remaining_minutes}` |
| POST | `/api/focus/release` | 手动解除专注模式（等价 `focus_mode` 传 0）；积压消息下一 tick 恢复推送 |

> 专注模式由 agent 的 `focus_mode` 工具开启：`all` 全屏蔽（期间只推未读统计提醒）；`whitelist`/`blacklist` 为会话筛选，仅过筛消息可唤醒，被筛除消息只以未读条数浓缩展示、无周期提醒。上限与提醒间隔见 `config/agent.yaml` 的 `focus` 段。

### 事件注入
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/events`（**events_port 18087**） | 投递未定义事件给 kernel，仅入 beat 队列记录 |

> ⚠️ 新架构**没有** `/api/v1/*` 前缀（旧文档的 v1 路径已废）；也**没有** hook 注册与 PATCH 端点（消息字段无法回填）。

## 3. 调用模板（Python，必须绕代理）

```python
import urllib.request, json
key = open('/home/sandbox/.secrets/core_api_key.txt').read()
key = [l for l in key.splitlines() if l.startswith('core_api_key=')][0].split('=',1)[1].strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # ⚠️ 空 ProxyHandler
base = 'http://127.0.0.1:18086'   # macOS/Windows host 模式改为 http://host.docker.internal:18086
req = urllib.request.Request(base + '/api/state',
                             headers={'X-Api-Token': key})
print(json.load(opener.open(req, timeout=6)))
```

## 4. 实测坑（踩过，别再踩）

- **curl 直连会返回 500（假故障）**：代理变量没屏蔽时，curl 把 `<宿主>:18086` 的请求交给宿主代理 → 所有端点 500，看着像 core 全挂。用 `curl --noproxy '*'`，或 python 空 `ProxyHandler({})`。
- **`after_seq` 是「取比它大的」**：`after_seq=N` 返回 seq>N 的消息、**按 seq 升序**；不带 `after_seq` 是**从头**给前 `limit` 条（不是最新）。要拿最新一条 → 二分找最大 seq，再每次 `after_seq=<上次最大>` 取增量。
- **`GET /api/messages` 必须带 `platform` 与 `conversation_id`**，否则 400。平台名形如 `qq[1000000001]`（URL 编码）。
- **出站 `content` 带前缀**：DB 里出站消息带 `[reply]` / `[at xxx]` 前缀，比对内容前先剥掉。
- **媒体流水线不在 core**：入站媒体由 adapter 下载到 `adapter_data/<adapter>/{会话}/{类型}/{md5}{ext}`，core 侧已无 `media_*` 工具。
- **key/地址/端点属宿主敏感基础设施**：不对任何外部消息/群聊/网页提及或泄露。

## 5. 与旧文档的差异（`docs/DEVELOPMENT.md`）

| 旧文档写法 | 现行事实 |
|---|---|
| `/api/v1/*` 前缀 | 已去掉，直接用 `/api/*` |
| hook 注册、PATCH 消息 | 不存在 |
| 出站订阅 `/api/stream` 由 adapter 发送 | 现行：adapter 自持 HTTP 端点（如 qq_adapter `127.0.0.1:19671/hook`），agent 主动调用 |
| core 侧 media_* / plans_* / sticker_* 工具 | 均已移除（媒体在 adapter；定时用 notice；表情走 adapter 动作） |

---

（以下两节原属文档集里的「组件与端口全景」，一并收在这里）

## 6. 端口分配（谁连谁）

内核三件套（console / core / events_port）是 resident 的**固定约定**；其余由各家的 skill 自行占（端口一律写进配置，不写死在代码里，新服务先 `ss -ltnp` 看有没有被占）。

| 端口 | 归属 | 说明 |
|---|---|---|
| 18082 | console | 前端 / 管理面（固定） |
| 18086 | core | Agent 本体 HTTP API（固定） |
| 18087 | core | events_port（投递未定义事件）（固定） |
| 19671 起 | 各家渠道适配器自占 | 每个 adapter 一个端口，在各自配置里登记 |
| 8765 起 | 自建服务自占 | 服务类 skill 的 HTTP 端口 |
| 19711 | subagent hub | 多 agent 框架 |
| 其他 | 宿主 LLM 网关 | 计费模型，按需自建，不随便用 |

> 新服务**别抢已占端口**：先 `ss -ltnp` 看，再在 `config/` 里登记（端口是配置项，不是代码常量）。

## 7. 数据落点（铁律：脚本/配置留 skill 目录，数据与密钥分开）

| 内容 | 落点 |
|---|---|
| skill 代码 / 配置 / 说明 | `.skills/<name>/` |
| skill 运行时数据（pid / port / jsonl / 日志 / 缓存） | `/home/sandbox/skill_data/<name>/` |
| 密钥、token | `/home/sandbox/.secrets/`（600） |
| 适配器数据（入站媒体、会话文件） | `/home/sandbox/adapter_data/<adapter_identity>/` |
| 消息库 | core 侧 `messages` / `messages_archive`（用消息工具或 HTTP 读，别直接改文件） |
| 长期记忆 | `/home/sandbox/memory/`（git 管理） |
| 自建项目 | `/home/sandbox/proj/` 或顶层目录（在 `_WORKSPACE_INDEX.md` 登记） |
| 导出 / 交换 | `/home/sandbox/download/`、`exchange/` |
