# actions — 一个动作一件事

目录约定：`actions/<动作名>/<动作名>.py`，一个脚本只干一件事，用法写在脚本首部 docstring。共用逻辑在 `actions/_hook.py`。

服务地址/端口/token 由 `_hook.py` 自己找（`skill_data/qq_adapter/port` → `QQ_ADAPTER_PORT` → 19671；token 走 `.secrets/`）。

| 动作 | 干什么 | 对应服务端 tag |
| --- | --- | --- |
| `group_list` | 列出我加入的所有群 | `get_group_list` |
| `group_member_list` | 列出一个群的全部成员 | `get_group_member_list` |
| `group_member_info` | 查某人在群里的信息（昵称/名片/身份/入群时间） | `get_group_member_info` |
| `group_info` | 查群基本信息 | `get_group_info` |
| `group_info_ex` | 查群扩展信息（群主/创建时间/人数上限等） | `get_group_info_ex` |
| `stranger_info` | 查陌生人资料（昵称/性别/等级/学校） | `get_stranger_info` |
| `user_status` | 查某人在线状态 | `get_user_status` |
| `friend_list` | 列出我的好友 | `get_friend_list` |
| `unidirectional_friend_list` | 列出单向好友（我加了对方、对方没加我） | `get_unidirectional_friend_list` |
| `get_msg` | 按消息 id 取那条消息 | `get_msg` |
| `group_msg_history` | 拉群聊历史消息 | `get_group_msg_history` |
| `group_notice` | 读群公告 | `get_group_notice` |
| `essence_list` | 列群精华消息 | `get_essence_msg_list` |
| `group_shut_list` | 列群禁言名单 | `get_group_shut_list` |
| `group_system_msg` | 取群系统消息（入群申请等，含真 flag） | `get_group_system_msg` |
| `group_ignore_add_request` | 列被忽略的入群申请 | `get_group_ignore_add_request` |
| `doubt_friends_add_request` | 列未处理的好友申请 | `get_doubt_friends_add_request` |
| `file_url` | 取群/私聊文件下载直链 | `get_file_url` |
| `group_root_files` | 列群文件（根目录） | `get_group_root_files` |
| `group_file_system_info` | 查群文件容量/数量概况 | `get_group_file_system_info` |
| `qun_album_list` | 列群相册 | `get_qun_album_list` |
| `group_signed_list` | 列群打卡名单 | `get_group_signed_list` |
| `emoji_likes` | 查某条消息的贴表情情况 | `get_emoji_likes` |
| `profile_like` | 查自己主页获赞数 | `get_profile_like` |
| `group_at_all_remain` | 查群里 @全体成员还剩几次 | `get_group_at_all_remain` |
| `send_forward` | 把某条消息转发到会话 | `send_forward` |
| `send_like` | 给某人资料卡点赞 | `send_like` |
| `set_msg_emoji_like` | 给某条消息贴表情 | `set_msg_emoji_like` |
| `group_sign` | 群打卡 | `group_sign` |
| `group_todo` | 把某条消息设成群待办 | `group_todo` |
| `kick` | 把成员移出群 | `kick` |
| `ban` | 群禁言（或解除） | `ban` |
| `set_card` | 改群名片 | `set_card` |
| `set_group_name` | 改群名 | `set_group_name` |
| `set_admin` | 设置/取消群管理员 | `set_admin` |
| `set_special_title` | 设专属头衔 | `set_special_title` |
| `set_group_remark` | 给群设备注 | `set_group_remark` |
| `set_friend_remark` | 给好友设备注 | `set_friend_remark` |
| `set_essence` | 把某条消息设为群精华 | `set_essence` |
| `delete_essence` | 取消某条消息的群精华 | `delete_essence` |
| `send_group_notice` | 发群公告 | `send_group_notice` |
| `delete_group_notice` | 删群公告 | `delete_group_notice` |
| `handle_friend_request` | 同意/拒绝好友申请 | `handle_friend_request` |
| `handle_group_request` | 同意/拒绝入群申请 | `handle_group_request` |
| `delete_friend` | 删好友（不可逆） | `delete_friend` |
| `set_group_leave` | 退群（或解散群） | `set_group_leave` |
| `delete_group_file` | 删群文件（不可逆） | `delete_group_file` |
| `delete_group_folder` | 删群文件目录（不可逆） | `delete_group_folder` |
| `attention_get` | 查当前注意力群。 | (手写/组合动作) |
| `attention_set` | 设置或关闭注意力群。 | (手写/组合动作) |
| `poke` | 戳一戳某人。群聊走 group_poke，私聊走 friend_poke。 | (手写/组合动作) |
| `recall` | 撤回一条消息。 | (手写/组合动作) |
| `send_file` | 往一个会话发一个文件。 | (手写/组合动作) |
| `send_image` | 往一个会话发一张图片。 | (手写/组合动作) |
| `send_text` | 往一个会话发一条纯文本消息。 | (手写/组合动作) |
| `status` | 看适配器状态。 | (手写/组合动作) |

`python3 _generate.py` 可按 `_spec.yaml` 补生成缺失脚本（`--list` 只看、`--force` 覆盖、`--only a b` 指定）；手写的动作不放进 `_spec.yaml` 就不会被覆盖。
