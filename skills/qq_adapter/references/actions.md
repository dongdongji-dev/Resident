# qq_adapter 参考：动作全集与 payload 模板

> 从 `SKILL.md` 拆出（2026-10-17 整理）。**日常调用看 `SKILL.md`**（send_text / qq_op）；要调别的动作或手写 payload 时看这里。

### 手动 curl 调 /hook（查询/群管等动作）

payload 支持 **JSON 动作结构**（推荐，等价于 XML）：

```bash
curl -s -X POST http://127.0.0.1:19671/hook \
  -H "X-Api-Token: sk-..." \
  -d '{
    "action": {
      "elements": [
        {"tag": "send_messages",
         "attributes": {"conversation_id": "group_200000001"},
         "content": [{"tag": "text", "attributes": {}, "content": [{"text": "你好"}]}]}
      ]
    }
  }'
```

> ⚠️ **坑（2026-08-22 19:16 实测）**：简化 text 快捷（content 里裸 `{"text":"..."}` 无 tag）对 **send_messages 无效**，报"message 没有可发送的段"。发送必须用上面带 `tag/attributes/content` 的完整段结构。裸 text 快捷仅部分查询类动作可能可用，发送一律用完整结构。

> ⚠️ **坑 2（2026-09-18 13:05 实测）：直接 curl /hook 时 `sticker` 段不能给裸文件名**。actions_definition 里写的 `<sticker file="aaa.png"/>`（在 `/workspace/sticker/` 下自动替换）**是走 core 侧 sticker_base 解析的**，绕开 core 直接打 19671 时文件名解析不到 → 该段被丢弃 → 整条报 **"message 没有可发送的段"**。**直连写法**：把表情当图片发，给沙箱绝对路径——
> ```json
> {"tag":"image","attributes":{"file":"/home/sandbox/sticker/xxx.gif"}}
> ```
> 实测 `{"tag":"text",...}` ＋ 该 image 段一条消息发出成功（msg 1740909321）。

> ⚠️ **坑 3（2026-10-17 08:0x 实测）：Python 侧 `actions/_hook.py` 的 `call()` 收的是「元素列表」，不是整个 payload**。正确写法 `call([element("get_group_msg_history", {"conversation_id": "group_xxx", "count": 20})])`；传成 `call({"action": {"elements": [...]}})` 会在服务端报 `'str' object has no attribute 'get'`——**错误信息与真实原因毫无关系，别顺着它查**。另：`call()` 会把结果 JSON 直接 print 到 stdout，脚本里要 `with contextlib.redirect_stdout(io.StringIO()):` 兜住，否则历史类动作的返回会把输出撑爆（实测被截断到只能看前几条）。

> **读转发消息内容（2026-09-06 实测）**：/hook 无 get_forward_msg 动作；群里的 [转发消息] 段 raw_segments 只有 `{"type":"forward","data":{"id":...}}`。要读内容直接连 Napcat WS：`ws://host.docker.internal:3001?access_token=testtoken123`（token 走 URL query 参数，**不是** set_token action，set_token 会被断开），收 hello 后发 `{"action":"get_forward_msg","params":{"message_id":"<forward id>"}}` 即返回转发链全部子消息（raw_message/sender/time）。

```bash
curl -s -X POST http://127.0.0.1:19671/hook \
  -H "X-Api-Token: sk-..." \
  -d '{"action": {"elements": [
    {"tag": "send_messages", "attributes": {"conversation_id": "group_200000001"},
     "content": [{"text": "你好"}]}
  ]}}'  # ← 仅示例，send_messages 请用上方完整段结构
```

XML 等价（老协议兼容）：

```bash
curl -s -X POST http://127.0.0.1:19671/hook \
  -H "X-Api-Token: sk-..." \
  -d '{"xml": "<action platform=\"qq[1000000001]\"><send_messages conversation_id=\"group_200000001\"><text>你好</text></send_messages></action>"}'
```

健康检查：`curl http://127.0.0.1:19671/status`


## 56 个动作（actions_definition.py 全集）

发送：send_messages（旧名 message 等价）/ send_forward / send_forward_nodes /
send_group_notice / send_like / group_sign / group_todo / set_msg_emoji_like / poke
管理：kick / ban / set_card / set_group_name / set_admin / set_special_title /
set_group_remark / set_friend_remark / set_essence / delete_essence /
delete_group_notice / delete_friend(高风险) / set_group_leave(高风险) /
delete_group_file(高风险) / delete_group_folder(高风险)
> ⚠️ **高风险动作默认禁用，要开必须写对位置（2026-10-11 实测踩坑）**：`config/local.yaml` 里
> `actions` 开关是**顶层键**（`raw["actions"]`），**不是** `napcat:` 段内的缩进子键——
> 写进 napcat 段（2 空格缩进）会**静默失效**（解析成默认全禁用，无任何报错）。要临时开：
> 顶层写
> ```yaml
> actions:
>   delete_group_file: true
> ```
> 然后**重启 adapter**（`bash start.sh --clean`，配置只在启动时读一次），
> 用完把 `actions: {}` 改回来再重启（恢复默认）。`delete_group_file` 属性要
> `group_id` + `file_id` + `busid`（群文件 busid=102，从 `get_group_root_files` 拿）。
> **踩坑记录**：改完不生效时先确认缩进层级，再看 `/proc/<pid>`/日志 `action disabled by config`。
审批：handle_friend_request / handle_group_request
> ⚠️ **入站通知里的 request 数字不是 flag（2026-09-18 07:50 实测踩坑）**：入站通知
> `conversation_key=...:request_group_1789688957_2501625778` 里那段数字是截断值，
> 拿它调 `handle_group_request` 会返回 `ok:true` 但**请求仍 checked=False（没生效）**。
> **真 flag 两条路可取**：① `get_group_system_msg`（count=50）的 `join_requests[].request_id`
> （本轮真值 1789688956083483，与通知前缀只前 10 位相同）；② **messages 库该条请求的 `raw_segments`**
> （`{"type":"request","data":{"flag":"1791453604504710",...}}`，2026-10-08 18:0x 实测——更直接、不必等系统消息刷新；用它 `handle_group_request flag=<该值>` 有效，与通知前缀同样只前 10 位相同）。
> 复核两路：
> ①`checked=True`；②`get_group_member_info` 返回成员体（含 `join_time`）。同坑适用于
> `handle_friend_request`（好友申请同理取 `get_doubt_friends_add_request` 的 flag）。
注意力：set_attention（切换注意力群，该群消息无需 @ 即唤醒；空串=关闭，
QQ 同时只有一个）/ get_attention（查当前注意力群）
查询：get_group_list / get_group_member_list / get_group_member_info /
get_group_info / get_group_info_ex / get_stranger_info / get_user_status /
get_friend_list / get_unidirectional_friend_list / get_msg /
get_group_msg_history / get_group_notice / get_essence_msg_list /
get_group_shut_list / get_group_system_msg / get_group_ignore_add_request /
get_doubt_friends_add_request / get_file_url / get_group_root_files /
get_group_file_system_info / get_qun_album_list / get_group_signed_list /
get_emoji_likes / get_profile_like / get_group_at_all_remain / get_status
删除：delete_msg

**poke（戳一戳）用法**（2026-08-27 23:45 实测）：独立元素，非消息段。
```json
{"action": {"elements": [
  {"tag": "poke", "attributes": {"conversation_id": "group_200000001", "target_id": "3043432015"}}
]}}
```
- 群聊走 `group_poke`，私聊走 `friend_poke`；需要 X-Api-Token（见 config/local.yaml api_key）。
- 回复"戳一戳"类互动用这个，别用文字硬回。

**at / reply / 混合段用法**（2026-08-27 补写）：
- **群里回复某人必须 at 对方**（QQ 不 at 不提醒，人可能去干别的了）。at 段：`{"tag": "at", "attributes": {"target_id": "对方QQ"}}`。
- reply 引用某条消息：`{"tag": "reply", "attributes": {"msg_id": "消息id"}}`。
- 撤回自己的消息：`{"tag": "delete_msg", "attributes": {"message_id": "消息id"}}`（type delete_msg，可撤自己发错的）。
- send_messages 的 content 可混排多个段，按顺序拼接：
```json
{"action": {"elements": [
  {"tag": "send_messages", "attributes": {"conversation_id": "group_200000001"},
   "content": [
     {"tag": "text", "attributes": {}, "content": [{"text": "答案在上面，看下"}]},
     {"tag": "at", "attributes": {"target_id": "2079701571"}}
   ]}
]}}
```
- 回复群成员求助/问答类消息时，默认带 at 或 reply 段，别裸发 text（除非是面向全群的公告式发言）。

高风险动作默认禁用，需在 config actions 段显式开启。

