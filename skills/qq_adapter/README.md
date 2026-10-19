# qq_adapter — QQ OneBot11 常驻适配器

任何 agent 拿到这份目录，配上自己的 `config/local.yaml` 与 `.secrets/`，就能用
**自己的号**登录、收发自己的消息。代码里不含任何身份信息。

## 结构

```
start.sh              # 一键起常驻服务（start / stop / status / --clean）
config/default.yaml   # 通用默认（零身份、零密钥）
config/local.yaml     # 本机覆盖（连接地址、白名单、端口）——不进分发包
src/qq_adapter/       # 常驻服务本体（python 包）
actions/              # 对该服务的一套操作，走 HTTP（actions/README.md 有索引）
  send_text/          #   发文本（--reply / --at / --force / heredoc / --from-file）
  send_flow/          #   多发串（按行或按 XML 元素切分，串行发送 + 回执核销）
  send_file/ send_image/ recall/ poke/ attention_set/ attention_get/ status/
  <动作名>/<动作名>.py  # 其余按动作名分目录，一个目录一件事
references/           # 参考文档（actions 全集 / 入站排障 / 账号安全取证）
tools/                # 辅助脚本（发送链取证）
tests/                # 回归测试（参数防呆、自己消息去重、媒体上限等）
requirements.txt      # aiohttp + PyYAML
```

数据（pid / log / port / attention.json / raw_events.jsonl）落在 `state_dir`
（默认 `skill_data/qq_adapter/`），**不在 skill 目录里**。

## 谁要用，怎么用

1. 依赖：`pip install -r requirements.txt`
2. 密钥：`.secrets/napcat_token.txt`（napcat WS token）、
   `.secrets/core_api_key.txt`（core 的 key，同时是 /hook 的鉴权 token）
   —— 也可用环境变量 `NAPCAT_TOKEN` / `CORE_API_KEY` 直接给。
3. 配置：改 `config/local.yaml` 里的 `ws_url` / `core.url` / 白名单。
4. 起服务：`bash start.sh`；停：`bash start.sh stop`；状态：`bash start.sh status`。
5. 看通没通：`actions/status/status.py` 或 `curl 127.0.0.1:19671/status`。

> ⚠️ 配置只在启动时读一次，改完要重启。

## 与 core 的接口

| 方向 | 接口 |
|---|---|
| 入站 | napcat WS 事件 → 白名单 → `POST core /api/messages`（带 sender_data） |
| 出站 | agent 调 `POST adapter /hook`（JSON 或 XML 等价），自持端点、无 hook 注册 |
| 仲裁 | 发送前查该会话未读数，超 `unread_threshold`（默认 5）拒绝，`force_exec="true"` 强制 |

动作全集（发送 / 管理 / 审批 / 查询 / 注意力 / 删除）见 `SKILL.md` 与
`actions/README.md`；高风险动作默认禁用，开关写在 `config/local.yaml` 的**顶层**
`actions:` 键（放 napcat 段里会静默失效）。
