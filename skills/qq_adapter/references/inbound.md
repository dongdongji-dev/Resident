# qq_adapter 参考：入站事件与文件传输

> 从 `SKILL.md` 拆出（2026-10-17 整理）。排障（消息没入库／重复入库／媒体下载异常）时看这里。

## 入站事件处理（handlers.py）

message（群/私聊）/ poke / recall / emoji_like / group_ban·increase·decrease·
admin / friend·group request / essence / group_upload / notify（honor·lucky_king·
group_name·profile_like）/ friend_add / bot_offline 全部入库，唤醒规则沿用老
项目语义（私聊恒唤醒；群聊 @bot/@all 或 reply 引用 bot 消息才唤醒）。

**入库记录规律（2026-09-24 修复后）**：私聊**出站**消息在 messages 库里**只应有一条**（`private_<对方QQ>`）。2026-09-09 曾观察到双记录（另一条落在 `private_<bot自己的QQ>`，如 1000000001）——那是 bug：NapCat `message_sent` 回显事件里 `user_id` 是 bot 自己、对方在 `target_id`，旧实现按 `user_id` 解析会话；同时与 `/hook` 出站上报重复。修复＝`outbound.py` 登记表去重 + `target_id` 解析（详见 `wiki/知识/机制/适配器与平台接入.md` 末节）。**若再见到 `private_<bot自己>` 的出站行，就是回归，先查 `handlers._private_conversation_id` 与登记表。** 历史遗留 1849 行重复待在 core 侧清理（未闭环）。

**空内容 message 事件 = 文件下载通知（2026-09-06 排障实锤）**：QQ 的
"对方已下载文件"通知会被 Napcat 当普通 message 推过来（`message=[]`、
`raw_message=""`、带 Napcat 特有字段 `real_seq`=真实会话序号），出现时机=
向对方发文件后数秒内。handlers.py 对这类事件（非自发、无文本、无 segments
或 segments 渲染空）合成 `[系统通知] 空内容消息事件...` 可读描述入库，不再
产生空白消息。异常原始事件（post_type=message 且无内容）经 __main__.py
`_dump_raw_event` 落盘 `state_dir/raw_events.jsonl`（config `raw_events` 段，
mode=anomalous，排障依据，09-06 00:42:46 首个实证条目）。

## 文件传输

- 小文件（≤ media_base64_max_bytes 5MB）：base64:// 直发
- 大文件：adapter fileserver（media_serve_url）→ napcat URL 拉取
- /file 端点流式字节转发（core→adapter→napcat upload_file_stream）

### 入站媒体下载超时（2026-09-13 整改）

- **症状**：QQ 发来的 PDF/大文件从不自动落盘，日志只有
  `WARNING handlers: media download timed out msg_id=...`，agent 只能手动补拉。
- **根因**：`handlers._download_media` 写死 **12s 总预算** ＋ `media.py`
  用**阻塞式** `urllib.request.urlopen`（顺带把整个事件循环卡住，心跳/其他
  入站事件一起停摆）。6.9MB/78MB 的 PDF 必然超时（实证 msg 1389753804 /
  139783277；09-08 三连超时同因）。
- **现状**：下载改 **aiohttp 异步流式**；超时**按段类型可配**——
  `config/local.yaml` 的 `napcat.media_download.timeouts`
  （默认 image 30 / record 120 / video 180 / file 600 秒，file 额外留 20s
  给 `get_*_file_url` 补拉）。预算 = 本条消息各段类型超时的最大值。
- **回归测试**：`proj/qq_adapter_tests/test_media_download.py`（60MB 全速
  md5 校验 / 限速下载期间事件循环不被阻塞 / 超时路径）
  ＋ `test_handlers_media.py`（handlers 集成：落盘注入 + media_index
  写库 + 幂等 + 混排 + 超时）。两脚本均**真实 HTTP**、无 mock。
  改 media.py / handlers 后先跑这两个脚本再重启 adapter。

### 入站媒体自动下载上限（2026-10-16 约定）

- **配置**：`napcat.media_download.max_mb`（默认 **50**，`0` = 不限制）。
- **超限行为**：声明大小（file 段 `size` / 图片等 `file_size`）超过上限就
  **不下载**，`seg.data.media_status = "oversize"`；消息里该段的 `url`
  改为**真实 http 直链**（不再是 `files/...` 内部占位），可直接手动下载。
- **notice**：超限或下载失败（`media_status = "failed"`）时，适配器给 agent
  发一条 notice（走 core `/api/notice`，不进 messages 库），列出段名/大小/
  原因/直链，并注明「直链是 QQ 侧临时地址、有时效」。
- **状态字段**：`media_status ∈ ok / oversize / no_url / failed`（写在
  `seg['data']` 上），渲染与 notice 都读它。
- **回归测试**：`tests/test_media_limits.py`（15 例，全离线：上限判定、
  直链渲染、notice 生成、无 submitter 不抛）。

