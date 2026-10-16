"""NapCat adapter action domain: the full XML operation protocol.

Every operation is expressed as an element inside ``<action>`` (hook
protocol); the adapter's ActionExecutor translates elements into OneBot11
API calls. Two classes:

动作元素无同步/异步之分——调用方式由 core 工具决定：
platform_action（异步，提交即返回，结果经 action_result 事件以用户名义
推回）/ platform_action_sync（同步，阻塞等待执行结果直接返回）。查询类
操作通常用同步，发送/管理类用异步。

The list below is registered with the core as the hook action domain
(description + examples enter the system prompt so the model knows the
exact XML protocol).
"""

# 高风险动作：配置中默认禁用（enable=false），需人工在 config/napcat.yaml
# actions 段显式打开。HTTP 回调收到这些 action 时直接拒绝（不执行）。
HIGH_RISK_ACTIONS = frozenset({
    "delete_friend",        # 删除好友（不可逆）
    "set_group_leave",      # 退群/解散群（解散不可逆）
    "delete_group_file",    # 删除群文件（不可逆）
    "delete_group_folder",  # 删除群文件目录（不可逆）
    "delete_group_notice",  # 删除群公告
    "set_admin",            # 设置群管理员（需群主）
})

# 动作域全集：发送/管理/审批/删除/查询（无同步异步之分，调用方式由
# core 工具决定：platform_action 异步 / platform_action_sync 同步）
#
# 返回协议（platform_action 的 action_result 事件内容）：
# - 成功    → "执行成功" + message_id（发送类）或 result（查询类）
# - 仲裁拒绝 → ok=false，errors=["仲裁拒绝：会话 xx 未读 N 条待处理，请先读取
#   未读消息了解全貌后再回复（如需强制执行可标注 force_exec=\"true\"）"]
# - 未启用  → ok=false，errors=["动作 xx 未启用（config/napcat.yaml actions
#   段未打开，或高风险动作默认禁用）"]
# - 权限不足/平台报错 → ok=false，errors=[平台返回原因]
#
# 仲裁规则：send 的 message 元素默认受未读仲裁（会话未读数 > unread_threshold
# 时拒绝，防模型高频主动发言打断用户）；message force_exec="true" 屏蔽仲裁
# 强制执行（仍过白名单与媒体检查）。
#
# 权限分级：set_admin / set_group_leave(dismiss=true) 需【群主】；
# kick / ban / set_special_title / send_group_notice / delete_group_notice /
# set_essence / delete_essence / delete_group_file / delete_group_folder
# 需【管理员】。
NAP_ACTIONS = [
    {
        "type": "send_messages",
        "description": "发送消息到会话——消息容器：conversation_id 属性（会话"
                       "定位 group_xxx/private_xxx）+ 子元素段（有序，可多个"
                       "text/poke/file 等）。子元素：<text>（QQ 文本框：文本与"
                       "内嵌元素混排保序——<reply msg_id=/> 引用（上限1）、"
                       '<at target_id=/>、<face id=/> QQ 内置表情、<image '
                       "file=沙箱完整路径/>、<sticker file=文件名/> 快速引用"
                       "（/workspace/sticker/ 下，自动替换，image 协议发送）；"
                       "文本含 < > & 需 XML 转义 &lt; &gt; &amp;）、<poke "
                       'target_id=/>、<image>/<record>/<video>/<file>（沙箱'
                       "路径|files/内部|宿主绝对路径）、<sticker>、<face>、"
                       "<dice>/<rps>、<json data=.../>。默认受未读仲裁：会话"
                       "未读超阈值会被拒绝（提示先读消息）；确认必须立即发送"
                       '加 force_exec="true"。',
        "examples": [
            # 强样例：一条消息覆盖文本框全部组合能力
            [
                '<action platform="qq[1000000001]"><send_messages '
                'conversation_id="group_200000002"><text><reply '
                'msg_id="123456"/><at target_id="1000000002"/>'
                '<face id="97"/>你好呀，在的哦&lt;reply&gt;是转义文本'
                '<image file="沙箱图片完整路径"/><sticker file="aaa.png"/>'
                '</text><poke target_id="1000000002"/><text>第二条消息</text>'
                '<sticker file="bbb.png"/><dice/></send_messages></action>',
                '预期结果：[1] <send_messages conversation_id="group_200000002">'
                ' 发送成功 msg_id=xxx（文本框内引用1条+@+QQ表情+图片+sticker'
                '+转义文本混排发送；戳一戳独立动作；第二条文本；sticker 快速'
                '引用自动替换发送；骰子发送。发送成功带 msg_id 回执）',
            ],
            # 强制发送：force_exec 加在 send_messages 元素属性上
            [
                '<action platform="qq[1000000001]"><send_messages '
                'conversation_id="group_200000002" force_exec="true">'
                '<text>紧急确认，请立即处理</text></send_messages></action>',
                '预期结果：[1] <send_messages conversation_id="group_200000002"'
                ' force 发送成功 msg_id=xxx（force_exec=true 屏蔽未读仲裁：'
                '会话未读超阈值也强制发送；不加 force_exec 时会被拒绝并提示'
                '先读取消息了解全貌）',
            ],
        ],
    },
    {
        "type": "send_forward",
        "description": "转发单条消息到目标会话。conversation_id（目标会话，"
                       "group_xxx/private_xxx）+ message_id（被转发消息 id，"
                       "须存在且对机器人可见）。注意：napcat 部分场景（消息不存在/权限不足等）可能返回执行成功但实际未生效，请以实际效果为准。风控提示：message_id 必须是真实存在的消息，严禁编造不存在的 id 或转发虚假消息（易触发账号风控）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><send_forward '
                'conversation_id="group_200000002" message_id="2113948433"/>'
                '</action>',
                '预期结果：[1] <send_forward conversation_id="group_'
                '200000002" message_id="2113948433"> 发送成功 msg_id=xxx'
                '（目标会话出现该消息，带 msg_id 回执）',
            ],
            [
                '<action platform="qq[1000000001]"><send_forward '
                'conversation_id="private_1000000002" message_id="999999"/>'
                '</action>',
                '预期结果：[1] <send_forward conversation_id="private_'
                '1000000002" message_id="999999"> 失败：无法找到消息999999'
                '（被转发消息不存在时拒绝，不会发送）',
            ],
        ],
    },
    {
        "type": "send_forward_nodes",
        "description": "合并转发到目标会话：conversation_id（group_xxx/"
                       "private_xxx）+ <node> 子元素列表（每个 node 为一条"
                       "消息：sender_id 发送者 QQ、sender_name 昵称、内嵌"
                       "text 等段；至少一个 node）。风控提示：node 的 sender_id/sender_name 必须真实，内容不得编造——伪造转发节点身份或转发虚假消息易触发账号风控；不要虚构不存在的消息来源。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><send_forward_nodes '
                'conversation_id="group_200000002"><node sender_id="123" '
                'sender_name="张三"><text>你好</text></node><node '
                'sender_id="456" sender_name="李四"><text>大家好</text></node>'
                '</send_forward_nodes></action>',
                '预期结果：[1] <send_forward_nodes conversation_id="group_'
                '200000002"> 发送成功 msg_id=xxx（群内出现合并转发卡片，'
                '含张三/李四两条节点，带 msg_id 回执）',
            ],
            [
                '<action platform="qq[1000000001]"><send_forward_nodes '
                'conversation_id="group_200000002"></send_forward_nodes>'
                '</action>',
                '预期结果：[1] <send_forward_nodes conversation_id="group_'
                '200000002"> 失败：send_forward_nodes 需要至少一个 <node> '
                '段（无节点时拒绝，不会发送）',
            ],
        ],
    },

    {
        "type": "kick",
        "description": "将成员移出群。conversation_id（group_xxx）+ user_id（被"
                       '踢 QQ）；reject_add="true" 时同时拒绝该成员再次申请。'
                       "【需管理员权限】注意：napcat 部分场景（消息不存在/权限不足等）可能返回执行成功但实际未生效，请以实际效果为准。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><kick conversation_id='
                '"group_200000002" user_id="3452499695"/></action>',
                '预期结果：[1] <kick conversation_id="group_200000002" user_id'
                '="3452499695"> 执行成功（成员被移出群，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><kick conversation_id='
                '"group_200000002" user_id="3452499695" reject_add="true"/>'
                '</action>',
                '预期结果：[1] <kick conversation_id="group_200000002" user_id'
                '="3452499695" reject_add="true"> 失败：权限不足（非管理员/'
                '群主时 napcat 拒绝，不会执行）',
            ],
        ],
    },
    {
        "type": "ban",
        "description": "群禁言。conversation_id（group_xxx）+ user_id + "
                       'duration=秒数（0=解除禁言）；whole="true" 时切换全员'
                       '禁言（enable="true"/"false" 开/关，忽略 user_id/'
                       "duration）。【需管理员权限】注意：napcat 部分场景（消息不存在/权限不足等）可能返回执行成功但实际未生效，请以实际效果为准。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><ban conversation_id='
                '"group_200000002" user_id="3452499695" duration="600"/>'
                '</action>',
                '预期结果：[1] <ban conversation_id="group_200000002" user_id'
                '="3452499695" duration="600"> 执行成功（成员被禁言10分钟，'
                '无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><ban conversation_id='
                '"group_200000002" whole="true" enable="true"/></action>',
                '预期结果：[1] <ban conversation_id="group_200000002" whole'
                '="true" enable="true"> 执行成功（全群禁言开启，无消息回执）',
            ],
        ],
    },
    {
        "type": "set_card",
        "description": "设置群名片。conversation_id（group_xxx）+ user_id + "
                       'card（名片内容，空=清除）。【需管理员权限】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_card conversation_id='
                '"group_200000002" user_id="1000000002" card="新名片"/>'
                '</action>',
                '预期结果：[1] <set_card conversation_id="group_200000002" '
                'user_id="1000000002" card="新名片"> 执行成功（群名片已设置，'
                '无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><set_card conversation_id='
                '"group_200000002" user_id="1000000002" card=""/></action>',
                '预期结果：[1] <set_card conversation_id="group_200000002" '
                'user_id="1000000002" card=""> 执行成功（群名片已清除）',
            ],
        ],
    },
    {
        "type": "set_group_name",
        "description": "修改群名称。conversation_id（group_xxx）+ name（新"
                       '群名）。【需管理员权限】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_group_name '
                'conversation_id="group_200000002" name="新群名测试"/>'
                '</action>',
                '预期结果：[1] <set_group_name conversation_id="group_'
                '200000002" name="新群名测试"> 执行成功（群名称已修改，无'
                '消息回执）',
            ],
        ],
    },
    {
        "type": "set_admin",
        "description": "设置/取消群管理员。conversation_id（group_xxx）+ "
                       'user_id + enable（"true"/"false" 设为/取消管理员）。'
                       "【需群主权限·高风险，默认禁用】",
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_admin '
                'conversation_id="group_200000002" user_id="1000000002" '
                'enable="true"/></action>',
                '预期结果：[1] <set_admin conversation_id="group_'
                '200000002" user_id="1000000002" enable="true"> 执行成功'
                '（该成员已设为群管理员；非群主时可能返回成功但实际未生效，'
                '以实际效果为准）',
            ],
        ],
    },
    {
        "type": "set_special_title",
        "description": "设置群成员专属头衔。conversation_id（group_xxx）+ "
                       'user_id + title（头衔内容，空=清除）。【需群主权限】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_special_title '
                'conversation_id="group_200000002" user_id="1000000002" '
                'title="测试头衔"/></action>',
                '预期结果：[1] <set_special_title conversation_id="group_'
                '200000002" user_id="1000000002" title="测试头衔"> 执行成功'
                '（群头衔已设置，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><set_special_title '
                'conversation_id="group_200000002" user_id="1000000002" '
                'title=""/></action>',
                '预期结果：[1] <set_special_title conversation_id="group_'
                '200000002" user_id="1000000002" title=""> 执行成功（群头衔'
                '已清除）',
            ],
        ],
    },
    {
        "type": "set_group_remark",
        "description": "设置群备注（给群起别名，仅自己可见）。conversation_id"
                       "（group_xxx）+ remark（别名内容）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_group_remark '
                'conversation_id="group_200000002" remark="测试群别名"/>'
                '</action>',
                '预期结果：[1] <set_group_remark conversation_id="group_'
                '200000002" remark="测试群别名"> 执行成功（群备注已设置，'
                '无消息回执）',
            ],
        ],
    },
    {
        "type": "set_friend_remark",
        "description": "设置好友备注（给好友起别名，跨群生效，仅自己可见）。"
                       "user_id + remark（备注内容）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_friend_remark '
                'user_id="1000000002" remark="测试好友别名"/></action>',
                '预期结果：[1] <set_friend_remark user_id="1000000002" '
                'remark="测试好友别名"> 执行成功（好友备注已设置，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><set_friend_remark '
                'user_id="999999999" remark="测试"/></action>',
                '预期结果：[1] <set_friend_remark user_id="999999999" '
                'remark="测试"> 失败：用户 999999999 不是好友（非好友时 '
                'napcat 拒绝，不会设置）',
            ],
        ],
    },
    {
        "type": "set_essence",
        "description": "设置群精华消息。conversation_id（group_xxx）+ "
                       "message_id（目标消息 id）。【需管理员权限】",
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_essence '
                'conversation_id="group_200000002" message_id="2113948433"/>'
                '</action>',
                '预期结果：[1] <set_essence conversation_id="group_'
                '200000002" message_id="2113948433"> 执行成功（消息已设'
                '为群精华，无消息回执）',
            ],
        ],
    },
    {
        "type": "delete_essence",
        "description": "取消群精华消息。conversation_id（group_xxx）+ "
                       "message_id（目标消息 id）。【需管理员权限】",
        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_essence '
                'conversation_id="group_200000002" message_id="2113948433"/>'
                '</action>',
                '预期结果：[1] <delete_essence conversation_id="group_'
                '200000002" message_id="2113948433"> 执行成功（消息已移出'
                '群精华，无消息回执）',
            ],
        ],
    },
    {
        "type": "send_group_notice",
        "description": "发布群公告。conversation_id（group_xxx）+ content"
                       '（公告正文）；pinned="1" 置顶。【需管理员权限】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><send_group_notice '
                'conversation_id="group_200000002" content="新公告内容"/>'
                '</action>',
                '预期结果：[1] <send_group_notice conversation_id="group_'
                '200000002" content="新公告内容"> 执行成功（群公告已发布，'
                '无消息回执）',
            ],
        ],
    },
    {
        "type": "delete_group_notice",
        "description": "删除群公告。conversation_id（group_xxx）+ notice_id"
                       '（get_group_notice 返回的公告 id）。【需管理员权限·'
                       '高风险，默认禁用】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_group_notice '
                'conversation_id="group_200000002" notice_id="notice_1"/>'
                '</action>',
                '预期结果：[1] <delete_group_notice conversation_id="group_'
                '200000002" notice_id="notice_1"> 执行成功（群公告已删除，'
                '无消息回执；未在 config 启用时返回未启用拒绝）',
            ],
        ],
    },
    {
        "type": "group_sign",
        "description": "群打卡签到。conversation_id（group_xxx）。每天一次，"
                       "重复打卡可能无效果。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><group_sign '
                'conversation_id="group_200000002"/></action>',
                '预期结果：[1] <group_sign conversation_id="group_'
                '200000002"> 执行成功（今日已打卡，无消息回执）',
            ],
        ],
    },
    {
        "type": "group_todo",
        "description": "把指定消息设为群待办（提醒成员处理）。conversation_id"
                       "（group_xxx）+ message_id（目标消息 id，须存在）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><group_todo '
                'conversation_id="group_200000002" message_id="2113948433"/>'
                '</action>',
                '预期结果：[1] <group_todo conversation_id="group_'
                '200000002" message_id="2113948433"> 执行成功（消息已设为'
                '群待办，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><group_todo '
                'conversation_id="group_200000002" message_id="9999999999"/>'
                '</action>',
                '预期结果：[1] <group_todo conversation_id="group_'
                '200000002" message_id="9999999999"> 失败：消息不存在'
                '（目标消息不存在时拒绝，不会设置）',
            ],
        ],
    },
    {
        "type": "delete_msg",
        "description": "撤回消息（仅能撤回机器人自己发送的消息）。"
                       "message_id（目标消息 id）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_msg '
                'message_id="2113948433"/></action>',
                '预期结果：[1] <delete_msg message_id="2113948433"> 执行'
                '成功（消息已撤回，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><delete_msg '
                'message_id="9999999999"/></action>',
                '预期结果：[1] <delete_msg message_id="9999999999"> 执行'
                '成功（消息不存在/非机器人消息时可能返回成功但实际未撤回，'
                '以实际效果为准）',
            ],
        ],
    },
    {
        "type": "handle_friend_request",
        "description": "处理好友申请。flag（申请凭据，来自入站 request_friend "
                       '消息 raw_segments）+ approve（"true" 同意 / "false" '
                       "拒绝）+ remark（同意时的备注，可选）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><handle_friend_request '
                'flag="f1" approve="true" remark="你好"/></action>',
                '预期结果：[1] <handle_friend_request flag="f1" '
                'approve="true" remark="你好"> 执行成功（申请已同意并备注，'
                '无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><handle_friend_request '
                'flag="f2" approve="false"/></action>',
                '预期结果：[1] <handle_friend_request flag="f2" '
                'approve="false"> 执行成功（申请已拒绝）',
            ],
        ],
    },
    {
        "type": "handle_group_request",
        "description": "处理入群申请。flag（申请凭据，来自入站 request_group "
                       '消息 raw_segments 或 get_group_system_msg）+ approve'
                       '（"true" 同意 / "false" 拒绝）+ reason（拒绝理由，'
                       "可选）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><handle_group_request '
                'flag="g1" approve="true"/></action>',
                '预期结果：[1] <handle_group_request flag="g1" '
                'approve="true"> 执行成功（入群申请已同意，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><handle_group_request '
                'flag="g2" approve="false" reason="群已满"/></action>',
                '预期结果：[1] <handle_group_request flag="g2" approve="false"'
                ' reason="群已满"> 执行成功（入群申请已拒绝）',
            ],
        ],
    },
    {
        "type": "send_like",
        "description": "给用户点赞（QQ 赞）。user_id + times（次数，默认 1，"
                       "过多可能触发频率限制）。风控提示：点赞受频率限制，请勿高频/大量点赞（易触发风控与限制）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><send_like '
                'user_id="1000000002" times="1"/></action>',
                '预期结果：[1] <send_like user_id="1000000002" times="1"> '
                '执行成功（已点赞，无消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><send_like '
                'user_id="999999999" times="1"/></action>',
                '预期结果：[1] <send_like user_id="999999999" times="1"> '
                '失败：点赞失败 被点赞 QQ 号非法（QQ 号不存在时拒绝）',
            ],
        ],
    },
    {
        "type": "set_msg_emoji_like",
        "description": "设置/取消消息的表情点赞（QQ 表情回应）。message_id + "
                       'emoji_id（表情 id）+ set（"true" 点赞 / "false风控提示：message_id/emoji_id 必须是真实存在的，勿编造（对不存在消息操作易触发风控）。" 取消）。',
        "examples": [
            [
                '<action platform="qq[1000000001]"><set_msg_emoji_like '
                'message_id="2113948433" emoji_id="97" set="true"/></action>',
                '预期结果：[1] <set_msg_emoji_like message_id="2113948433" '
                'emoji_id="97" set="true"> 执行成功（消息已加上表情回应，无'
                '消息回执）',
            ],
            [
                '<action platform="qq[1000000001]"><set_msg_emoji_like '
                'message_id="9999999999" emoji_id="97" set="true"/></action>',
                '预期结果：[1] <set_msg_emoji_like message_id="9999999999" '
                'emoji_id="97" set="true"> 失败：msg not found（消息不存在时'
                '拒绝）',
            ],
        ],
    },
    {
        "type": "delete_friend",
        "description": "删除好友。user_id（目标 QQ）。【高风险·不可逆，默认禁用，谨慎使用】",

        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_friend '
                'user_id="1000000002"/></action>',
                '预期结果：[1] <delete_friend user_id="1000000002"> 执行'
                '成功（好友已删除；默认禁用，未启用时返回未启用拒绝）',
            ],
        ],
    },
    {
        "type": "set_group_leave",
        "description": "退出群。conversation_id（group_xxx）+ dismiss（true=解散群【需群主·不可逆】/ 缺省 false 仅退群）。【高风险，默认禁用】",


        "examples": [
            [
                '<action platform="qq[1000000001]"><set_group_leave '
                'conversation_id="group_200000002" dismiss="false"/></action>',
                '预期结果：[1] <set_group_leave conversation_id="group_'
                '200000002" dismiss="false"> 执行成功（机器人已退出群；默认'
                '禁用，未启用时返回未启用拒绝）',
            ],
        ],
    },
    {
        "type": "delete_group_file",
        "description": "删除群文件。conversation_id（group_xxx）+ file_id（"
                       'get_group_root_files 返回）。【需管理员权限·不可逆，'
                       '默认禁用】',
        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_group_file '
                'conversation_id="group_200000002" file_id="fid1"/></action>',
                '预期结果：[1] <delete_group_file conversation_id="group_'
                '200000002" file_id="fid1"> 执行成功（群文件已删除；默认'
                '禁用，未启用时返回未启用拒绝）',
            ],
        ],
    },
    {
        "type": "delete_group_folder",
        "description": "删除群文件目录。conversation_id（group_xxx）+ "
                       "folder_id（目录 id）。【需管理员权限·不可逆，默认禁用】",
        "examples": [
            [
                '<action platform="qq[1000000001]"><delete_group_folder '
                'conversation_id="group_200000002" folder_id="fid2"/></action>',
                '预期结果：[1] <delete_group_folder conversation_id="group_'
                '200000002" folder_id="fid2"> 执行成功（群文件目录已删除；'
                '默认禁用，未启用时返回未启用拒绝）',
            ],
        ],
    },
    {
        "type": "get_group_info",
        "description": "获取群信息（名称/人数/全员禁言等）。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_info conversation_id="group_200000002"/></action>', '预期结果：返回群信息（group_name/member_count/max_member_count/全员禁言状态）']
        ],
    },
    {
        "type": "get_group_info_ex",
        "description": "获取群详细信息扩展。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_info_ex conversation_id="group_200000002"/></action>', '预期结果：返回群详细信息扩展（扩展字段）']
        ],
    },
    {
        "type": "get_group_list",
        "description": "获取群列表（同步）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_list/></action>', '预期结果：返回群列表（群号/群名/人数）']
        ],
    },
    {
        "type": "get_group_member_list",
        "description": "获取群成员列表（同步）。conversation_id（group_xxx）。",
        "examples": [
            [
                '<action platform="qq[1000000001]"><get_group_member_list '
                'conversation_id="group_200000002"/></action>',
                '预期结果：返回群成员列表（user_id/nickname/card/role）',
            ],
        ],
    },
    {
        "type": "get_group_member_info",
        "description": "获取群成员信息（同步）。conversation_id + user_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_member_info conversation_id="group_200000002" user_id="1000000002"/></action>', '预期结果：返回指定成员信息（user_id/nickname/card/role/权限）']
        ],
    },
    {
        "type": "get_stranger_info",
        "description": "获取用户资料（昵称/性别/等级/学校等）。user_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_stranger_info user_id="1000000002"/></action>', '预期结果：返回用户资料（昵称/性别/等级/学校/在线状态等）']
        ],
    },
    {
        "type": "get_user_status",
        "description": "获取用户在线状态。user_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_user_status user_id="1000000002"/></action>', '预期结果：返回用户在线状态（status/ext_status）']
        ],
    },
    {
        "type": "get_friend_list",
        "description": "获取好友列表（含备注）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_friend_list/></action>', '预期结果：返回好友列表（user_id/nickname/remark/sex/level）']
        ],
    },
    {
        "type": "get_unidirectional_friend_list",
        "description": "获取单向好友列表（对方加了你）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_unidirectional_friend_list/></action>', '预期结果：返回单向好友列表（对方加了你，你未加对方）']
        ],
    },
    {
        "type": "get_msg",
        "description": "获取单条消息详情（同步）。message_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_msg message_id="2113948433"/></action>', '预期结果：返回消息详情（sender/content/时间等完整字段）']
        ],
    },
    {
        "type": "get_group_msg_history",
        "description": "获取群消息历史（同步）。conversation_id + count（条数）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_msg_history conversation_id="group_200000002" count="20"/></action>', '预期结果：返回群消息历史（count 条，含 sender/content）']
        ],
    },
    {
        "type": "get_group_notice",
        "description": "获取群公告列表。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_notice conversation_id="group_200000002"/></action>', '预期结果：返回群公告列表（notice_id/sender_id/内容/发布时间）']
        ],
    },
    {
        "type": "get_essence_msg_list",
        "description": "获取群精华消息列表。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_essence_msg_list conversation_id="group_200000002"/></action>', '预期结果：返回群精华消息列表（message_id/sender/内容/设置者）']
        ],
    },
    {
        "type": "get_group_shut_list",
        "description": "获取群禁言成员列表。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_shut_list conversation_id="group_200000002"/></action>', '预期结果：返回群禁言成员列表（user_id/nickname/shut_up_time）']
        ],
    },
    {
        "type": "get_group_system_msg",
        "description": "获取加群请求/邀请列表（轮询，入群申请不主动推送）。"
                       "count=数量。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_system_msg count="50"/></action>', '预期结果：返回加群请求/邀请列表（request_id/申请者/群/验证信息）']
        ],
    },
    {
        "type": "get_group_ignore_add_request",
        "description": "获取被忽略的加群请求。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_ignore_add_request/></action>', '预期结果：返回被忽略的加群请求列表']
        ],
    },
    {
        "type": "get_doubt_friends_add_request",
        "description": "获取可疑好友申请列表。count=数量。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_doubt_friends_add_request count="50"/></action>', '预期结果：返回可疑好友申请列表（user_id/nickname/flag）']
        ],
    },
    {
        "type": "get_file_url",
        "description": "获取文件下载 URL。conversation_id（群/私聊）+ file_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_file_url conversation_id="group_200000002" file_id="fid1"/></action>', '预期结果：返回文件下载 URL（url 字段，供沙箱内下载）']
        ],
    },
    {
        "type": "get_group_root_files",
        "description": "获取群根目录文件列表（file_id/file_name/size/uploader）。"
                       "conversation_id + file_count。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_root_files conversation_id="group_200000002" file_count="50"/></action>', '预期结果：返回群根目录文件列表（file_id/file_name/size/uploader）+ 文件夹']
        ],
    },
    {
        "type": "get_group_file_system_info",
        "description": "获取群文件系统信息（总数/空间）。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_file_system_info conversation_id="group_200000002"/></action>', '预期结果：返回群文件系统信息（file_count/limit_count/空间）']
        ],
    },
    {
        "type": "get_qun_album_list",
        "description": "获取群相册列表。conversation_id + attach_info（分页）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_qun_album_list conversation_id="group_200000002"/></action>', '预期结果：返回群相册列表（album_id/album_name/封面/时间，attach_info 分页）']
        ],
    },
    {
        "type": "get_group_signed_list",
        "description": "获取群今日打卡列表。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_signed_list conversation_id="group_200000002"/></action>', '预期结果：返回群今日打卡列表（user_id/nick/time/rank）']
        ],
    },
    {
        "type": "get_emoji_likes",
        "description": "获取消息表情点赞列表。message_id + emoji_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_emoji_likes message_id="2113948433" emoji_id="97"/></action>', '预期结果：返回消息表情点赞列表（user_id/nick_name）']
        ],
    },
    {
        "type": "get_profile_like",
        "description": "获取资料点赞信息。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_profile_like/></action>', '预期结果：返回资料点赞信息（总赞数/最近点赞人）']
        ],
    },
    {
        "type": "get_group_at_all_remain",
        "description": "获取群@全体剩余次数。conversation_id。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_group_at_all_remain conversation_id="group_200000002"/></action>', '预期结果：返回@全体剩余次数（can_at_all/剩余次数）']
        ],
    },
    {
        "type": "get_status",
        "description": "获取运行状态（在线/良好）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_status/></action>', '预期结果：返回运行状态（online/good）']
        ],
    },
    {
        "type": "set_attention",
        "description": "切换注意力群（运行时状态，持久化）——设置后该群所有新消息一律走"
                       "wakeup（无需 @），QQ 同时只能有一个注意力群；旧群自动释放。"
                       "conversation_id=\"group_xxx\"；显式传空串 conversation_id=\"\""
                       " 表示关闭注意力（回到默认：仅私聊/@/reply 唤醒）。",
        "examples": [
            ['<action platform="qq[1000000001]"><set_attention conversation_id="group_200000001"/></action>',
             '预期结果：注意力切到 group_200000001，该群新消息全部唤醒 agent（返回 previous/current）'],
            ['<action platform="qq[1000000001]"><set_attention conversation_id=""/></action>',
             '预期结果：关闭注意力（previous=原群，current=空）'],
        ],
    },
    {
        "type": "get_attention",
        "description": "查询当前注意力群（空串=未设置）。",
        "examples": [
            ['<action platform="qq[1000000001]"><get_attention/></action>',
             '预期结果：返回当前注意力群 conversation_id（如 group_200000001）'],
        ],
    },
]


# XML 协议头：层级/嵌套/编法说明（注入系统提示词，模型据此生成动作 XML）
XML_PROTOCOL = """<action> 根元素：platform 属性必填（platform="qq[1000000001]" 指向 bot 实例）。
一个 <action> 可含多个动作子元素（顺序执行，结果逐元素返回）。

动作元素两种形态：
a) 纯属性（参数=XML 属性，自闭合）：
   <kick conversation_id="group_123" user_id="456"/>
b) 带子元素（嵌套段，send_messages 与 send_forward_nodes）：
   <send_messages conversation_id="group_123"><text><reply msg_id="9"/><at target_id="456"/><face id="97"/>你好</text><poke target_id="456"/><sticker file="aaa.png"/></send_messages>
   <send_forward_nodes conversation_id="group_123"><node sender_id="1" sender_name="张三"><text>内容</text></node></send_forward_nodes>

会话索引：conversation_id 统一表示会话——group_xxx（群）/ private_xxx（私聊），
群/私聊差异由适配器自动分流，模型无需区分。

send_messages 内子元素（有序，可多个）：<text>（QQ 文本框：文本与内嵌元素
混排保序——<reply msg_id=/> 上限1、<at target_id=/>、<face id=/>、<image
file=沙箱完整路径/>、<sticker file=文件名/> 快速引用；文本含 < > & 需转义
&lt; &gt; &amp;）、<poke target_id=/>（戳一戳）、<image>/<record>/<video>/
<file>（沙箱路径|files/内部|宿主绝对路径，自动流式传输）、<sticker>、
<face>、<dice>/<rps>、<json data=.../>。

仲裁：message 默认受未读仲裁（会话未读超阈值会被拒绝，提示先读消息）；确认
必须立即发送时加 force_exec="true" 强制执行。

结果：动作执行结果通过系统通知返回，格式 [序号] <元素开头> 发送成功 msg_id=
xxx / 失败原因；未启用的动作（高风险默认禁用）直接返回拒绝。"""
