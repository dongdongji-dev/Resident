# qq_adapter 参考：账号安全取证（tools/）

> 从 `SKILL.md` 拆出（2026-10-17 整理）。判断「某条以本账号名义发出的消息是不是我发的」时看这里。

## 账号安全取证：`tools/`（2026-10-15 立）

起因＝发现「同账号的另一个登录端以我的名义在群里发言」（详见 memory `raw/2026_10_15/当日发生事件.md` 与 `wiki/经验/技术备忘·长期教训.md`）。

- `tools/self_message_watch.py` —— 常驻监听 NapCat WS（默认 `ws://host.docker.internal:3001`，token 取 `.secrets/napcat_token.txt`），把 `message` / `message_sent` 事件**原文**追加到 `<skill_data>/qq_adapter/sent_watch.jsonl`。启动：`nohup python3 tools/self_message_watch.py &`（参数 `--ws/--token-file/--out` 可覆盖；常驻会长胖，`--max-bytes` 默认 5 MB 自动滚动成 `<out>.1`，只留一代）。
- `tools/verify_self_send.py <msg_id>...` —— 判定一条「以本账号名义发出的消息」是否真的由本 adapter 发出：在 `adapter.log` 里查发送链（`sent group conv=` / `hook action executed`），**缺链 ⇒ 非本 adapter 发出 ⇒ 同账号存在另一个登录端**。加 `--show` 打印命中日志行。
- 回归：`python3 tests/test_verify_self_send.py`（3 例 + 窗口边界）。

配套判据：NapCat 的 `get_msg` 返回 `post_type=message_sent` + `message_sent_type="self"` 只说明「NapCat 认为是本账号发出的」，**不等于**「本 adapter 发出的」——必须再用 `adapter.log` 配对。反面教训：`get_online_clients` 在本 NapCat 版本返回 `data: []`，不能拿它当反证。
