"""Inbound event handlers - the message branch first (owner workflow:
one branch at a time, verified against the real napcat).

message / message_sent (group + private) -> MessageDraft:
- conversation_id: group_{group_id} / private_{user_id}
- sender: raw sender object (nickname/card/role), identity core-side
- content: raw_message (CQ 码原始保留), llm_content: 段渲染保序
- direction: message_sent (is_self_send) -> out
- wakeup: 私聊默认 true；群聊 @bot/@all 或 reply 才 true（配置可关）
"""

from __future__ import annotations

from typing import Any

import asyncio
import logging

from qq_adapter.events import EventContext, register_event
from qq_adapter.message import MessageDraft, assemble_llm_content
from qq_adapter import outbound

# 媒体真实根（与 media.py 一致，不再用根目录软连接路径）
from qq_adapter.media import _WORKSPACE_ROOT as WORKSPACE_ROOT

logger = logging.getLogger("qq_adapter.handlers")


def _wakeup_for(event: dict[str, Any], ctx: EventContext,
                is_self: bool) -> bool:
    """唤醒触发（私聊/注意力群/@/reply，勿瞎搞）：
    1. 私聊；2. 群聊 = 当前注意力群（attention，无需 @ 全量唤醒）；
    3. 群聊 @bot / @all；4. 群聊 reply 且被引用消息是 bot 发出的。"""
    if is_self:
        return False
    # 注意力群优先：当前关注群内消息一律唤醒（不依赖 @/mention 开关）
    attention_group = ""
    if ctx.attention is not None:
        try:
            attention_group = str(ctx.attention.get() or "")
        except Exception:  # noqa: BLE001
            attention_group = ""
    if attention_group and event.get("message_type") == "group":
        conversation_id = f"group_{event.get('group_id', '')}"
        if conversation_id == attention_group:
            return True
    if not ctx.config_wakeup_on_mention:
        return False
    if event.get("message_type") == "private":
        return True
    for seg in event.get("message") or []:
        if seg.get("type") == "at" and seg.get("data", {}).get("qq") \
                in (ctx.self_id, "all"):
            return True
        if seg.get("type") == "reply":
            msg_id = str((seg.get("data") or {}).get("id", ""))
            if msg_id and _is_bot_message(ctx, msg_id):
                return True
    return False


def _is_bot_message(ctx: EventContext, remote_message_id: str) -> bool:
    """被引用消息是否为 bot 发出的。

    走 adapter 内存登记（qq_adapter.outbound）：出站上报时登记 message_id。
    原先查 core 的 by-remote 端点，但 core 未实现它（`/api/messages/by-remote`
    2026-09-24 实测 404）→ 这条判断恒为 False，引用唤醒从来没触发过。
    登记表是 LRU（默认 1000 条），覆盖最近发出的消息；更早的保守判否。"""
    return outbound.known(remote_message_id)


def _already_recorded_out(event: dict[str, Any]) -> bool:
    """message_sent 回显去重：这条消息是 /hook 出站上报登记过的吗？

    自己发出的消息有两条入库路径：①/hook 出站上报（actions._report_out，会话
    正确、即时）；②NapCat 的 message_sent 回显（本 handler）。旧实现两条都写，
    且回显那条 conversation_id 解析错（private_<bot 自己>），于是每条私聊消息
    在库里会存成两份（回显件与出站上报各一条）。

    登记表在 adapter 内存里，进程重启会丢——这时回显会走到会话解析分支，
    解析出的会话与 ① 一致，core 按 conversation_key 去重兜住，不会重复。"""
    return outbound.known(event.get("message_id"))


def _private_conversation_id(event: dict[str, Any], is_self: bool,
                             ctx: EventContext) -> str:
    """私聊会话 id 解析（2026-09-24 修复）。

    入站：user_id 就是对方。自己发出的（post_type=message_sent）：NapCat 把
    user_id 填成 bot 自己，真正的对方在 target_id（NapCat 事件约定：target_id
    = 目标 QQ/群号）——旧实现一律取 user_id，于是自己发的私聊消息全被写进
    `private_<bot 自己>` 这个假会话。取两个字段里第一个不等于 self_id 的值；
    都拿不到（协议变更）则丢弃该条，绝不写假会话。"""
    self_id = str(ctx.self_id or "")
    candidates = ([event.get("target_id"), event.get("user_id")]
                  if is_self else [event.get("user_id")])
    for raw in candidates:
        peer = str(raw or "").strip()
        if peer and peer != self_id:
            if is_self:
                logger.info("self private event: user_id=%s target_id=%s "
                            "→ conversation=private_%s msg_id=%s",
                            event.get("user_id"), event.get("target_id"),
                            peer, event.get("message_id"))
            return f"private_{peer}"
    logger.warning(
        "self private message 无法解析对方（user_id=%s target_id=%s "
        "self_id=%s msg_id=%s）→ 丢弃，不写假会话",
        event.get("user_id"), event.get("target_id"), self_id,
        event.get("message_id"))
    return ""


async def _handle_message(event: dict[str, Any], ctx: EventContext):
    message_type = event.get("message_type", "")
    is_self = event.get("post_type") == "message_sent" \
        or bool(event.get("is_self_send"))
    # 自己发出的消息：/hook 已入库一份，回显这份直接丢（见 _already_recorded_out）
    if is_self and _already_recorded_out(event):
        logger.info("self message_sent %s 已在 core（out）→ 跳过重复入库"
                    "（user_id=%s target_id=%s）",
                    event.get("message_id"), event.get("user_id"),
                    event.get("target_id"))
        return None
    if message_type == "group":
        conversation_id = f"group_{event.get('group_id', '')}"
        conversation_type = "group"
    else:
        conversation_id = _private_conversation_id(event, is_self, ctx)
        if not conversation_id:
            return None
        conversation_type = "private"

    sender = dict(event.get("sender") or {})
    if not sender.get("nickname") and sender.get("card"):
        sender["nickname"] = sender["card"]  # 群名片回退
    if is_self:
        sender["identity_id"] = "agent"  # bot 自己发出的消息标记 agent 身份
    ctx.current_conversation_id = conversation_id  # 渲染上下文（媒体段内 url）
    ctx.names[str(event.get("user_id", ""))] = \
        str(sender.get("card") or sender.get("nickname")
            or event.get("user_id", ""))

    segments = event.get("message") or []
    # 入站媒体前置下载（提交前完成，2026-08-25 整改）：新 core 无 PATCH 回填，
    # 提交后拿不到正确 url，故先下载再渲染——url = 真实落盘路径。失败/超时
    # 不阻断提交（渲染退回 files/ 内部占位）。
    if not is_self and ctx.loop is not None:
        await _download_media(event, segments, conversation_id, ctx)
        # 转发无内联（只有 id）：前置 get_forward_msg 拉取 → data.content 注入
        # → assemble_llm_content 时 render_forward 直接输出完整 JSON
        for seg in segments:
            if seg.get("type") == "forward" and seg.get("data", {}) \
                    and not seg["data"].get("messages") \
                    and not seg["data"].get("content"):
                await _resolve_forward_inline(event, seg, ctx)
                break
    llm_content = assemble_llm_content(segments, ctx)
    # 空内容 message 事件（2026-09-06 排障实锤：Napcat 把 QQ 的
    # "对方已下载文件"通知当 message 推过来，message=[]、raw_message=""，
    # 还带 Napcat 特有字段 real_seq=真实会话序号）。原样入库=空消息，
    # 唤醒预览空白且丢失"文件已被下载"这一信息 → 合成可读描述
    # （事实=空事件；推断=文件下载通知，明示是推断）。
    if not is_self and not llm_content \
            and not str(event.get("raw_message") or "").strip():
        if not segments:
            real_seq = event.get("real_seq")
            seq_note = f"，real_seq={real_seq}" if real_seq not in (None, "") \
                else ""
            llm_content = (
                "[系统通知] 空内容消息事件（无文本、无 segments"
                f"{seq_note}）——实测均出现在向对方发送文件后数秒内，"
                "应为『对方下载了你发的文件』一类通知（QQ 未给出显式"
                "内容标识，此为时序推断）")
        else:
            seg_types = ",".join(sorted(
                {str(s.get("type")) for s in segments}))
            llm_content = (
                f"[系统通知] 空内容消息事件（segments 类型={seg_types}，"
                "渲染后无文本）")
    draft = MessageDraft(
        platform=ctx.platform,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        remote_message_id=str(event.get("message_id", "")),
        sender=sender,
        content=str(event.get("raw_message", "")),
        llm_content=llm_content,
        raw_segments=[{"type": seg.get("type"), "data": seg.get("data", {})}
                      for seg in segments],
        wakeup=_wakeup_for(event, ctx, is_self),
        direction="out" if is_self else "in",
        created_at=_unix_to_local(event.get("time")),
    )
    # record 段：语音转写（napcat fetch_ptt_text + 3/10/30 重试，V2 方案）。
    # 官方转写有延迟，保持异步；结果写 media_index.json（无 PATCH 回填）。
    if not is_self and ctx.client is not None and ctx.loop is not None:
        for seg in draft.raw_segments:
            if seg.get("type") == "record":
                ctx.loop.create_task(_transcribe_record(event, seg, ctx))
                break
    return draft


async def _resolve_forward_inline(event: dict, seg: dict,
                                  ctx: EventContext) -> None:
    """转发前置拉取：get_forward_msg → nodes 注入 seg["data"]["content"]。

    渲染在 assemble_llm_content 时自然完成（render_forward 见 content 直接
    输出完整 JSON；转发内媒体段由 render_media 渲染，已下载时带真实路径）。
    新 core 无 PATCH 回填（2026-08-25 实证 404），故必须在提交前完成；
    拉取失败/超时不阻断提交（渲染退回 [转发消息] 占位）。"""
    try:
        nodes = await asyncio.wait_for(
            _fetch_forward_nodes(event, seg, ctx), timeout=12)
        if not nodes:
            # 一次重试（2026-10-02：adapter 首次 prefetch 曾 5s 超时，而同一 id
            # 直连只需 0.01~0.17s ⇒ 偶发慢/竞争；重试一次便宜且能显著降假阴性）
            nodes = await asyncio.wait_for(
                _fetch_forward_nodes(event, seg, ctx), timeout=12)
        if nodes:
            seg["data"]["content"] = nodes
            logging.getLogger("qq_adapter.handlers").info(
                "forward prefetched msg_id=%s nodes=%d",
                event.get("message_id"), len(nodes))
        else:
            logging.getLogger("qq_adapter.handlers").warning(
                "forward prefetch empty msg_id=%s", event.get("message_id"))
    except asyncio.TimeoutError:
        logging.getLogger("qq_adapter.handlers").warning(
            "forward prefetch timeout msg_id=%s", event.get("message_id"))
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter.handlers").exception(
            "forward prefetch failed")


async def _fetch_forward_nodes(event: dict, seg: dict,
                               ctx: EventContext) -> list[dict]:
    """get_forward_msg 原始节点列表（无则返回 []）。"""
    forward_id = str((seg.get("data") or {}).get("id", ""))
    if not forward_id:
        return []
    result = await ctx.client.api("get_forward_msg",
                                  {"message_id": forward_id})
    nodes = ((result.get("data") or {}).get("messages")
             or (result.get("data") or {}).get("content") or [])
    return [n for n in nodes if isinstance(n, dict)]


# 语音描述的状态语义（勿混淆）：
#   "暂无"                  = 未完成（转写中/失败待重试）
#   "[语音无文本内容]"       = 转写完成但无文本（音乐/语气/无内容音频）
#   实际文本                = 转写结果
NO_TEXT_DESCRIPTION = "[语音无文本内容]"


async def _transcribe_record(event: dict, seg: dict,
                             ctx: EventContext) -> None:
    """语音转写：fetch_ptt_text（QQ 官方转写，可能未就绪）→ 成功后结果写
    media_index.json（record 条目加 description）。

    新 core 无 PATCH /media/description 端点（2026-08-25 实证均 404），
    消息文本无法回填——转写结果落在媒体索引，agent 处理语音消息时读
    media_index.json 获取。转写"成功但无文本"的语音（音乐/语气等）标记为
    [语音无文本内容]，与"未识别"（暂无）严格区分。"""
    try:
        ok, text = await _fetch_ptt_text(
            ctx, str(event.get("message_id", "")))
        if not ok:
            # 失败：后台重试（3/10/30s），仍失败保持"暂无"（未完成）
            for delay in (3, 10, 30):
                await asyncio.sleep(delay)
                ok, text = await _fetch_ptt_text(
                    ctx, str(event.get("message_id", "")))
                if ok:
                    break
        if not ok:
            logging.getLogger("qq_adapter.handlers").info(
                "record transcription unavailable msg_id=%s",
                event.get("message_id"))
            return
        description = text if text else NO_TEXT_DESCRIPTION
        seg["data"]["description"] = description
        _write_transcription_to_index(
            ctx, str(event.get("message_id", "")), description)
        logging.getLogger("qq_adapter.handlers").info(
            "record transcribed msg_id=%s text=%s",
            event.get("message_id"), description[:80])
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter.handlers").exception(
            "record transcription failed")


def _write_transcription_to_index(ctx: EventContext, msg_id: str,
                                  description: str) -> None:
    """语音转写结果写入 media_index.json（对应 msg_id 的 record 条目）。"""
    import json
    import os
    conv_dir = str(getattr(ctx, "current_conversation_id", "") or "")
    if not conv_dir:
        return
    index_path = os.path.join(WORKSPACE_ROOT, conv_dir, "media_index.json")
    if not os.path.exists(index_path):
        return
    try:
        with open(index_path, encoding="utf-8") as fh:
            index = json.load(fh) or {}
        entry = index.get(msg_id)
        if not entry:
            return
        for m in entry.get("media") or []:
            if m.get("type") == "record":
                m["description"] = description
        with open(index_path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1)
        logging.getLogger("qq_adapter.handlers").info(
            "transcription indexed msg_id=%s text=%s", msg_id,
            description[:80])
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter.handlers").exception(
            "transcription index write failed")


async def _fetch_ptt_text(ctx: EventContext,
                          message_id: str) -> tuple[bool, str]:
    """napcat fetch_ptt_text。返回 (success, text)：
    success=True 表示转写完成（text 可能为空=无文本内容）；
    success=False 表示失败（未完成，可重试）。"""
    try:
        result = await ctx.client.api("fetch_ptt_text",
                                      {"message_id": message_id})
    except Exception:  # noqa: BLE001
        return False, ""
    if result.get("status") == "ok" and result.get("retcode") == 0:
        return True, str((result.get("data") or {}).get("text") or "").strip()
    return False, ""


async def _try_fetch_file_url(event: dict, seg: dict,
                              ctx: EventContext,
                              attempts: int = 3) -> str:
    """文件真实下载 url 补拉：群 get_group_file_url / 私聊 get_private_file_url。

    file_id 两种格式兼容：data.file_id 优先（get_msg 历史拉取格式），
    次 data.file（WS 实时事件格式通常是 file 字段即 file_id）。
    返回 url 字符串；失败/无 url 返回 ""。不依赖 core PATCH（新 core
    无 PATCH 端点，回填修复已于 2026-08-25 移除——媒体链路 adapter 侧闭环）。

    ⚠️ 2026-10-15 加重试（默认 3 次，退避 1.5s / 3s）：napcat 的
    `get_*_file_url` 是偶发慢而非必失败——00:24:45 约定发 aaa.txt 时单次
    15s 超时导致文件没落盘（`file url fetch failed`），我在 ws 上手工补拉
    一次即成功。重试期间消息处理停在本协程（不阻塞其它事件循环任务）。"""
    file_id = str(seg["data"].get("file_id")
                  or seg["data"].get("file") or "").strip()
    if not file_id:
        return ""
    if event.get("message_type") == "group":
        action = "get_group_file_url"
        params = {"group_id": int(event["group_id"]), "file_id": file_id}
    else:
        action = "get_private_file_url"
        params = {"user_id": int(event["user_id"]), "file_id": file_id}

    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        try:
            result = await ctx.client.api(action, params)
        except Exception:  # noqa: BLE001
            logger.exception("file url fetch failed (attempt %d/%d)",
                             attempt, total)
        else:
            url = str((result.get("data") or {}).get("url", ""))
            if url:
                if attempt > 1:
                    logger.info("file url fetched on attempt %d/%d",
                                attempt, total)
                return url
            logger.warning("file url empty (attempt %d/%d): %s",
                           attempt, total, action)
        if attempt < total:
            await asyncio.sleep(1.5 * attempt)
    return ""


def _unix_to_local(unix_seconds: Any) -> str:
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(int(unix_seconds)))
    except (TypeError, ValueError):
        return ""


def _render_poke_text(event: dict, ctx: EventContext) -> str:
    """poke 文本：``sender 动作 目标 自定义文本``（raw_info 的 nor 段拼装，
    V2 规则：第一个 nor=动作，后续=自定义文本；img 段丢弃）。"""
    user_a = ctx.name_of(event.get("user_id", ""))
    target_b = ctx.name_of(event.get("target_id", ""))
    action, custom = "", ""
    for item in event.get("raw_info") or []:
        if not isinstance(item, dict) or item.get("type") != "nor":
            continue
        txt = str(item.get("txt") or "").strip()
        if not txt:
            continue
        if not action:
            action = txt
        else:
            custom = txt
    return " ".join(p for p in (user_a, action, target_b, custom) if p)


def _handle_poke(event: dict, ctx: EventContext) -> MessageDraft:
    """戳一戳（notice/notify/poke）。

    语义（owner-confirmed）：
    - 自己戳自己 = 自己发送的消息（direction=out），不唤醒；
    - 被戳对象是 bot → 唤醒；
    - 别人戳别人 → 不入库？不——戳是消息，入库但不唤醒；
    - poke 无平台 msg_id → remote_message_id 由 adapter 生成。
    """
    self_id = str(ctx.self_id or "")
    user_id = str(event.get("user_id", ""))
    target_id = str(event.get("target_id", ""))
    is_self_poke = bool(self_id) and user_id == self_id
    is_poked_self = bool(self_id) and target_id == self_id

    if event.get("group_id"):
        conversation_id = f"group_{event.get('group_id')}"
        conversation_type = "group"
    else:
        conversation_id = f"private_{user_id}"
        conversation_type = "private"
    # poke 无平台消息 id：生成稳定 id（时间+双方）
    remote_id = f"poke_{event.get('time', '')}_{user_id}_{target_id}"

    sender = {"user_id": event.get("user_id", ""),
              "nickname": str(event.get("sender", {}) or {})
              .get("nickname", "") if event.get("sender") else ""}
    raw_segments = [{"type": "poke",
                     "data": {"id": target_id,
                              "raw_info": event.get("raw_info") or []}}]
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        remote_message_id=remote_id,
        message_type="poke",
        sender=sender,
        llm_content=_render_poke_text(event, ctx),
        raw_segments=raw_segments,
        wakeup=is_poked_self and not is_self_poke,
        direction="out" if is_self_poke else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _render_recall_text(event: dict) -> str:
    """撤回渲染（V2 规则）：
    群代撤：[消息撤回] {operator} 撤回了 {author} 的消息（msg_id=Y）
    其他：  [消息撤回] {operator} 撤回了消息（msg_id=Y）"""
    notice_type = str(event.get("notice_type", ""))
    message_id = str(event.get("message_id", ""))
    operator = str(event.get("operator_id") or event.get("user_id") or "")
    if notice_type == "group_recall":
        author = str(event.get("user_id") or "")
        if author and author != operator:
            return f"[消息撤回] {operator} 撤回了 {author} 的消息（msg_id={message_id}）"
    return f"[消息撤回] {operator} 撤回了消息（msg_id={message_id}）"


def _handle_recall(event: dict, ctx: EventContext) -> MessageDraft:
    """消息撤回（group_recall / friend_recall）。

    - remote_message_id = 原消息 id（与原消息同 key，message_type=recall 共存）；
    - direction：operator==bot → out（自己操作）；
    - 唤醒：被撤回的原消息是 bot 发出的（direction=out）→ 唤醒，并附
      原文预览（llm_content）。"""
    notice_type = str(event.get("notice_type", ""))
    group_id = event.get("group_id")
    user_id = str(event.get("user_id", ""))
    operator = str(event.get("operator_id") or user_id)
    message_id = str(event.get("message_id", ""))
    self_id = str(ctx.self_id or "")

    if notice_type == "group_recall" and group_id:
        conversation_id = f"group_{group_id}"
        conversation_type = "group"
    else:
        conversation_id = f"private_{user_id}"
        conversation_type = "private"

    # 被撤回原消息是 bot 发的 → 唤醒 + 原文预览（登记表里存了出站时的文本）
    is_bot_msg = bool(message_id) and _is_bot_message(ctx, message_id)
    preview = outbound.preview(message_id) if is_bot_msg else ""

    text = _render_recall_text(event)
    if preview:
        text += f"\n被撤回消息原文：{preview}"

    sender = {"user_id": operator}
    raw_segments = [{"type": "recall",
                     "data": {"message_id": message_id,
                              "operator_id": operator,
                              "user_id": user_id}}]
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        remote_message_id=message_id,
        message_type="recall",
        sender=sender,
        llm_content=text,
        raw_segments=raw_segments,
        wakeup=is_bot_msg,
        direction="out" if (self_id and operator == self_id) else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_emoji_like(event: dict, ctx: EventContext) -> MessageDraft:
    """消息表情点赞（group_msg_emoji_like）。

    - 每个表情独立一条消息：remote_message_id = {message_id}_{emoji_id}
      （同一条消息的不同表情各自入库；同一表情重复贴 → 同 key 去重）；
    - 渲染可读文本；不唤醒（仅入库）。"""
    group_id = event.get("group_id")
    user_id = str(event.get("user_id", ""))
    message_id = str(event.get("message_id", ""))
    likes = event.get("likes") or []
    is_add = bool(event.get("is_add"))

    if group_id:
        conversation_id = f"group_{group_id}"
        conversation_type = "group"
    else:
        conversation_id = f"private_{user_id}"
        conversation_type = "private"

    if not likes:
        likes = [{"emoji_id": "", "count": ""}]
    drafts = []
    for like in likes:
        if not isinstance(like, dict):
            continue
        emoji_id = str(like.get("emoji_id") or "")
        count = str(like.get("count")) if like.get("count") is not None \
            else ""
        action = "为本条消息添加了表情" if is_add else "为本条消息取消了表情"
        text = (f"{user_id} {action}（{emoji_id}"
                + (f"×{count}" if count else "")
                + f"，msg_id={message_id}）")
        drafts.append(MessageDraft(
            platform=ctx.platform,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            # key 含 add 状态：添加与取消各自入库（互不抢占）
            remote_message_id=(f"{message_id}_{emoji_id}_"
                               f"{'add' if is_add else 'del'}"),
            message_type="emoji_like",
            sender={"user_id": user_id},
            llm_content=text,
            raw_segments=[{"type": "emoji_like", "data": {
                "message_id": message_id, "emoji_id": emoji_id,
                "count": count, "add": str(is_add).lower()}}],
            wakeup=False,
            direction="in",
            created_at=_unix_to_local(event.get("time")),
        ))
    # 多个表情：前几条直接提交，最后一条返回（dispatch 统一提交）
    for draft in drafts[:-1]:
        ctx.submitter.submit(draft)
    return drafts[-1] if drafts else None


def _handle_group_notice(event: dict, ctx: EventContext) -> MessageDraft:
    """群管理事件：禁言/入群退群/管理员变更。

    - message_type = notice_type（group_ban/group_increase/...）；
    - 唤醒：被操作对象（user_id）是 bot → 唤醒；操作者和被操作者
      都是 bot 本体 → 不唤醒（自操作）；
    - direction：操作者是 bot → out（bot 执行的操作 = 自己发送的消息）。"""
    notice_type = str(event.get("notice_type", ""))
    sub_type = str(event.get("sub_type", ""))
    group_id = event.get("group_id")
    user_id = str(event.get("user_id", ""))
    operator = str(event.get("operator_id") or user_id)
    self_id = str(ctx.self_id or "")
    duration = str(event.get("duration", ""))
    if not group_id:
        return None  # 群管理事件必须有群

    is_target_self = bool(self_id) and user_id == self_id
    is_self_operator = bool(self_id) and operator == self_id
    target = ctx.name_of(user_id)
    actor = ctx.name_of(operator)

    if notice_type == "group_ban":
        if sub_type == "lift_ban":
            text = f"{actor} 解除了 {target} 的禁言"
        else:
            text = f"{actor} 禁言了 {target}" + (f"（{duration}秒）"
                                                 if duration else "")
    elif notice_type == "group_increase":
        text = f"{target} 加入了群" + ("（邀请）" if sub_type == "invite"
                                       else "（同意）")
    elif notice_type == "group_decrease":
        if sub_type == "kick_me":
            text = "该 agent 被踢出了群"
        elif sub_type == "disband":
            text = "群已解散"
        elif sub_type == "kick":
            text = f"{target} 被 {actor} 踢出了群"
        else:
            text = f"{target} 退出了群"
    elif notice_type == "group_admin":
        # OneBot group_admin 事件无 operator_id：无法得知操作者，
        # 渲染不带操作者；唤醒 = 被操作对象是 bot（无自操作判定）
        text = (f"{target} 被设为管理员"
                if sub_type == "set" else f"{target} 被取消管理员")
        operator = user_id
        is_self_operator = False  # 无操作者信息，direction 恒 in
    else:
        return None

    # remote_message_id 生成（无平台 id）
    remote_id = f"{notice_type}_{sub_type}_{event.get('time', '')}_{user_id}"
    # 唤醒（owner-confirmed）：
    # - bot 自操作（operator==bot）恒不提醒（防回环，操作对象是谁都不提醒）；
    # - 管理员变更/入群/退群/被踢（非 bot 操作）→ 恒提醒；
    # - group_ban（禁言，非 bot 操作）→ 被操作对象是 bot 才唤醒
    if is_self_operator:
        wakeup = False
    elif notice_type == "group_ban":
        wakeup = is_target_self
    else:
        wakeup = True
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=f"group_{group_id}",
        conversation_type="group",
        remote_message_id=remote_id,
        message_type=notice_type,
        sender={"user_id": operator},
        llm_content=text,
        raw_segments=[{"type": "notice",
                       "data": {"notice_type": notice_type,
                                "sub_type": sub_type,
                                "user_id": user_id,
                                "operator_id": operator,
                                "duration": duration}}],
        wakeup=wakeup,
        direction="out" if is_self_operator else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_request(event: dict, ctx: EventContext) -> MessageDraft:
    """好友/入群申请（request.friend / request.group）。

    - 恒唤醒（申请需处理）；flag 保留（审批凭据）；
    - request.friend：private_{user_id}；request.group：group_{group_id}。"""
    request_type = str(event.get("request_type", ""))
    user_id = str(event.get("user_id", ""))
    comment = str(event.get("comment", ""))
    flag = str(event.get("flag", ""))
    group_id = event.get("group_id")
    sub_type = str(event.get("sub_type", ""))

    if request_type == "friend":
        conversation_id = f"private_{user_id}"
        conversation_type = "private"
        text = f"{user_id} 申请加好友" + (f"（留言：{comment}）" if comment else "")
        message_type = "request_friend"
    else:
        if group_id:
            conversation_id = f"group_{group_id}"
            conversation_type = "group"
        else:
            conversation_id = f"private_{user_id}"
            conversation_type = "private"
        kind = "申请加入群" if sub_type == "add" else "邀请入群"
        text = f"{user_id} {kind}" + (f"（留言：{comment}）" if comment else "")
        message_type = "request_group"

    remote_id = f"request_{request_type}_{event.get('time', '')}_{user_id}"
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        remote_message_id=remote_id,
        message_type=message_type,
        sender={"user_id": user_id},
        llm_content=text,
        raw_segments=[{"type": "request", "data": {
            "request_type": request_type, "user_id": user_id,
            "comment": comment, "flag": flag, "sub_type": sub_type,
            "group_id": str(group_id or "")}}],
        wakeup=True,
        direction="in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_essence(event: dict, ctx: EventContext) -> MessageDraft:
    """群精华设置/删除（essence add/delete）。

    - 低频事件，有就触发；bot 自操作不触发（防回环）；
    - remote_message_id = 被设精华消息 id（同 key，message_type 共存）。"""
    sub_type = str(event.get("sub_type", ""))
    group_id = event.get("group_id")
    message_id = str(event.get("message_id", ""))
    sender_id = str(event.get("sender_id")
                    or event.get("operator_id")
                    or event.get("user_id") or "")
    self_id = str(ctx.self_id or "")
    if not group_id:
        return None
    is_self_operator = bool(self_id) and sender_id == self_id
    actor = ctx.name_of(sender_id)
    action = "设置了群精华" if sub_type == "add" else "取消了群精华"
    text = f"{actor} {action}（msg_id={message_id}）"
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=f"group_{group_id}",
        conversation_type="group",
        # key 含 sub_type：设置与取消各自入库（互不抢占）
        remote_message_id=f"{message_id}_{sub_type}",
        message_type="essence",
        sender={"user_id": sender_id},
        llm_content=text,
        raw_segments=[{"type": "essence", "data": {
            "message_id": message_id, "sub_type": sub_type,
            "sender_id": sender_id}}],
        wakeup=not is_self_operator,
        direction="out" if is_self_operator else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_group_upload(event: dict, ctx: EventContext) -> MessageDraft:
    """群文件上传（group_upload）。

    - 低频，有就触发；bot 上传不触发（防回环）。"""
    group_id = event.get("group_id")
    user_id = str(event.get("user_id", ""))
    self_id = str(ctx.self_id or "")
    file_info = event.get("file") or {}
    name = str(file_info.get("name", ""))
    size = file_info.get("size")
    file_id = str(file_info.get("id", ""))
    if not group_id:
        return None
    is_self_operator = bool(self_id) and user_id == self_id
    uploader = ctx.name_of(user_id)
    size_text = f"{int(size)}字节" if size not in (None, "") else ""
    text = f"{uploader} 上传了文件 {name}" + (f"（{size_text}）" if size_text else "")
    remote_id = (f"group_upload_{event.get('time', '')}_{user_id}_"
                 f"{name}")
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=f"group_{group_id}",
        conversation_type="group",
        remote_message_id=remote_id,
        message_type="group_upload",
        sender={"user_id": user_id},
        llm_content=text,
        raw_segments=[{"type": "group_upload", "data": {
            "file_id": file_id, "name": name, "size": size,
            "user_id": user_id}}],
        wakeup=not is_self_operator,
        direction="out" if is_self_operator else "in",
        created_at=_unix_to_local(event.get("time")),
    )


_HONOR_NAMES = {"talkative": "龙王", "performer": "群聊之火",
                "emotion": "快乐源泉"}


def _handle_notify(event: dict, ctx: EventContext) -> MessageDraft:
    """notify 子事件：honor（荣誉）/ lucky_king（运气王）/
    group_name（群名变更）/ profile_like（资料点赞）。

    唤醒（owner-confirmed）：honor/group_name 提醒；lucky_king/profile_like
    仅入库不提醒。"""
    sub_type = str(event.get("sub_type", ""))
    group_id = event.get("group_id")
    user_id = str(event.get("user_id", ""))
    self_id = str(ctx.self_id or "")
    target = ctx.name_of(user_id)

    if sub_type == "honor":
        honor_type = str(event.get("honor_type", ""))
        honor_name = _HONOR_NAMES.get(honor_type, honor_type or "荣誉")
        text = f"{target} 获得了荣誉（{honor_name}）"
        wakeup = True
        message_type = "honor"
    elif sub_type == "lucky_king":
        text = f"{target} 是红包运气王"
        wakeup = False
        message_type = "lucky_king"
    elif sub_type == "group_name":
        name_new = str(event.get("name_new", ""))
        text = f"{target} 将群名改为 {name_new}"
        wakeup = True
        message_type = "group_name"
    elif sub_type == "profile_like":
        text = f"{target} 点赞了你的资料"
        wakeup = False
        message_type = "profile_like"
    else:
        return None

    if not group_id:
        group_id = ""
    remote_id = f"notify_{sub_type}_{event.get('time', '')}_{user_id}"
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=f"group_{group_id}" if group_id else f"private_{user_id}",
        conversation_type="group" if group_id else "private",
        remote_message_id=remote_id,
        message_type=message_type,
        sender={"user_id": user_id},
        llm_content=text,
        raw_segments=[{"type": "notify", "data": {
            "sub_type": sub_type, "user_id": user_id,
            "honor_type": event.get("honor_type", ""),
            "name_new": event.get("name_new", ""),
            "name_old": event.get("name_old", "")}}],
        wakeup=wakeup,
        direction="out" if (self_id and user_id == self_id) else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_friend_add(event: dict, ctx: EventContext) -> MessageDraft:
    """好友添加成功（friend_add）：触发提醒。"""
    user_id = str(event.get("user_id", ""))
    self_id = str(ctx.self_id or "")
    remote_id = f"friend_add_{event.get('time', '')}_{user_id}"
    return MessageDraft(
        platform=ctx.platform,
        conversation_id=f"private_{user_id}",
        conversation_type="private",
        remote_message_id=remote_id,
        message_type="friend_add",
        sender={"user_id": user_id},
        llm_content=f"{user_id} 添加了 Bot 为好友",
        raw_segments=[{"type": "friend_add", "data": {"user_id": user_id}}],
        wakeup=True,
        direction="out" if (self_id and user_id == self_id) else "in",
        created_at=_unix_to_local(event.get("time")),
    )


def _handle_bot_offline(event: dict, ctx: EventContext) -> MessageDraft:
    """bot 离线（bot_offline）：触发提醒（agent 需知道社交媒体离线）。"""
    message = str(event.get("message", ""))
    tag = str(event.get("tag", ""))
    text = "机器人已离线" + (f"（{tag}）" if tag else "")         + (f"：{message}" if message else "")
    remote_id = f"bot_offline_{event.get('time', '')}"
    return MessageDraft(
        platform=ctx.platform,
        conversation_id="qq_self",
        conversation_type="self",
        remote_message_id=remote_id,
        message_type="bot_offline",
        sender={"user_id": ctx.self_id or ""},
        llm_content=text,
        raw_segments=[{"type": "bot_offline",
                       "data": {"tag": tag, "message": message}}],
        wakeup=True,
        direction="in",
        created_at=_unix_to_local(event.get("time")),
    )


async def _download_media(event: dict, raw_segments: list[dict],
                         conversation_id: str,
                         ctx: Any = None) -> None:
    """媒体段下载到本地工作区 + 原地注入 local_path/md5 + 写媒体索引。

    2026-08-25 整改（提交前调用）：①segs 不拷贝 data——download_message_media
    的注入（local_path/md5）直接反映到原始段，render_media 渲染真实路径；
    ②总超时保护——补拉 file url + 下载都可能慢，超时跳过不阻断提交；
    ③新 core 无 PATCH 回填，下载失败时渲染退回 files/ 内部占位。

    2026-09-13 整改：超时不再写死 12s，改为**按段类型**取预算
    （image 30 / record 120 / video 180 / file 600，可在配置
    media_download.timeouts 覆盖）——旧 12s 预算让 6.9MB/78MB 的 PDF
    必然超时（实证 msg 1389753804 / 139783277）。下载本身改异步流式，
    不再阻塞事件循环。"""
    import asyncio
    from qq_adapter.media import budget_for
    if event.get("_media_downloaded"):
        return
    event["_media_downloaded"] = True
    types = [s.get("type") for s in (raw_segments or [])
             if s.get("type") in ("image", "record", "video", "file")]
    overrides = getattr(ctx, "media_download_timeouts", None) or {}
    budget = budget_for(types, overrides)
    if not budget:
        return
    try:
        await asyncio.wait_for(
            _download_media_inner(event, raw_segments, conversation_id, ctx),
            timeout=budget)
    except asyncio.TimeoutError:
        logging.getLogger("qq_adapter.handlers").warning(
            "media download timed out msg_id=%s types=%s budget=%.0fs",
            event.get("message_id"), types, budget)
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter.handlers").exception(
            "media download task failed")


async def _download_media_inner(event: dict, raw_segments: list[dict],
                                conversation_id: str,
                                ctx: Any = None) -> None:
    """媒体下载主体（含 file url 补拉 + 落盘 + 索引）。"""
    import asyncio
    from qq_adapter.media import download_message_media
    segs = [{"type": s.get("type"), "data": s.get("data", {})}
            for s in (raw_segments or [])
            if s.get("type") in ("image", "record", "video", "file")]
    if not segs:
        return
    # file 段无 url → 补拉真实 url（异步 API，失败不阻断）
    if ctx is not None and getattr(ctx, "client", None) is not None:
        for seg in segs:
            if seg["type"] == "file" and not seg["data"].get("url") \
                    and (seg["data"].get("file")
                         or seg["data"].get("file_id")):
                url = await _try_fetch_file_url(event, seg, ctx)
                if url:
                    seg["data"]["url"] = url
                    logging.getLogger("qq_adapter.handlers").info(
                        "file url fetched msg_id=%s file=%s",
                        event.get("message_id"),
                        seg["data"].get("file", ""))
    max_mb = float(getattr(ctx, "media_download_max_mb", 0) or 0)
    max_bytes = int(max_mb * 1048576) if max_mb > 0 else None
    paths = await download_message_media(
        segs, conversation_id, asyncio.get_running_loop(),
        getattr(ctx, "media_download_timeouts", None) or None,
        max_bytes)
    # 超上限跳过 / 下载失败 → 发 notice 提醒（附真实直链，可手动下载）
    _notify_media_issue(event, conversation_id, segs, ctx)
    if not paths:
        return
    # 下载成功（download_message_media 已原地注入 data.local_path/md5）
    flat = []
    for seg in segs:
        md5 = seg["data"].get("md5", "")
        path = seg["data"].get("local_path", "")
        if md5 and path:
            flat.append({"type": seg["type"], "md5": md5, "path": path})
    # 写媒体索引（真实根 adapter_data，见 WORKSPACE_ROOT）
    import json
    import os
    conv_dir = f"group_{conversation_id}" if not \
        conversation_id.startswith("group_") and not \
        conversation_id.startswith("private_") else conversation_id
    index_path = os.path.join(WORKSPACE_ROOT, conv_dir,
                              "media_index.json")
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    index = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as fh:
                index = json.load(fh) or {}
        except Exception:  # noqa: BLE001
            index = {}
    # 按消息 id 记录
    msg_id = str(event.get("message_id", ""))
    index[msg_id] = {"time": _unix_to_local(event.get("time")),
                     "conversation_id": conversation_id,
                     "media": flat}
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    logging.getLogger("qq_adapter.handlers").info(
        "media downloaded msg_id=%s n=%d", msg_id, len(flat))


def _notify_media_issue(event: dict, conversation_id: str,
                        segs: list[dict], ctx: Any = None) -> None:
    """媒体未落盘（超上限跳过 / 下载失败）→ 给 agent 发一条 notice，附直链。

    2026-10-16 设计约定三条：①设自动下载大小上限；②超限/未落盘时消息里
    的路径改成**实际 url 直链**（渲染侧做，见 segments.render_media）；
    ③下载失败要 **notice 提醒**（并给出直链）。直链是 QQ 侧临时地址、
    有时效，所以 notice 里写明「要用趁早下载」。
    """
    submitter = getattr(ctx, "submitter", None)
    if submitter is None:
        return
    problems = [s for s in segs
                if (s.get("data") or {}).get("media_status") in
                ("oversize", "failed")]
    if not problems:
        return
    max_mb = float(getattr(ctx, "media_download_max_mb", 0) or 0)
    lines = []
    names = []
    for seg in problems:
        data = seg.get("data") or {}
        status = data.get("media_status")
        name = str(data.get("file", "") or "媒体")
        names.append(name)
        try:
            size = int(data.get("size") or data.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        size_text = f"{size / 1048576:.1f} MB" if size else "大小未知"
        if status == "oversize":
            head = f"超过自动下载上限（{max_mb:g} MB），未落盘"
        else:
            head = "下载失败，未落盘"
        entry = f"· [{seg.get('type')}] {name}（{size_text}）：{head}"
        url = str(data.get("url", ""))
        if url.startswith(("http://", "https://")):
            entry += f"\n  直链（QQ 侧临时地址、有时效）：{url}"
        lines.append(entry)
    content = (f"[媒体未落盘] 会话 {conversation_id} 消息 "
               f"{event.get('message_id', '')}，{len(problems)} 个段：\n"
               + "\n".join(lines)
               + "\n（要用就趁早下载——直链过期只能让对方重发；"
                 "上限可在配置 media_download.max_mb 调。）")
    notification = "[媒体未落盘] " + "、".join(names)[:80]
    try:
        submitter.create_notice(content, notification=notification)
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter.handlers").exception(
            "media issue notice failed")


def register_all() -> None:
    """注册全部事件分支（幂等；测试里清表后可重入）。"""
    for _type in ("message", "message_sent"):
        register_event(_type, "group")(_handle_message)
        register_event(_type, "private")(_handle_message)
    register_event("notice", "poke", "")(_handle_poke)
    register_event("notice", "", "notify")(_handle_poke)
    # 撤回：sub_type 位与 notice_type 位双注册（napcat 事件两种形态兼容）
    register_event("notice", "group_recall", "")(_handle_recall)
    register_event("notice", "friend_recall", "")(_handle_recall)
    register_event("notice", "", "group_recall")(_handle_recall)
    register_event("notice", "", "friend_recall")(_handle_recall)
    register_event("notice", "", "group_msg_emoji_like")(_handle_emoji_like)
    for _notice in ("group_ban", "group_increase", "group_decrease",
                    "group_admin"):
        register_event("notice", "", _notice)(_handle_group_notice)
    register_event("request", "friend", "")(_handle_request)
    register_event("request", "group", "")(_handle_request)
    register_event("notice", "", "essence")(_handle_essence)
    register_event("notice", "", "group_upload")(_handle_group_upload)
    for _notify in ("honor", "lucky_king", "group_name", "profile_like"):
        register_event("notice", _notify, "")(_handle_notify)
    register_event("notice", "", "friend_add")(_handle_friend_add)
    register_event("notice", "", "bot_offline")(_handle_bot_offline)


register_all()
