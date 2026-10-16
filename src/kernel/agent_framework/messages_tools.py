"""Message query tools for the Agent (messages domain).

- messages_query:        通用只读 SQL 筛选（SELECT-only 防注入）
- messages_conversations:会话（thread）列表 + 每会话最新 N 条（content 截断）
- messages_detail:       按 threads 返回全字段消息；任一超限 → 整体拒绝
- messages_download:     按 threads 把全部消息记录导出到运行时 download 目录
- messages_threads:      列出所有出现过的会话
- messages_unread:       按会话查看未读消息（取出即标记已读）
- messages_range:        按会话 + 时间区间查询（取出即标记已读）

除 messages_unread 只取未读入站消息外，conversations / range / detail /
download / query 都**同时包含双方消息**（direction=out 为自己发送的），
输出带 direction 字段便于区分。

thread = "platform:conversation_id"（如 qq:group_870375059），会话标识；
conversation_key（三段含消息 id）是消息级唯一键，注意区分。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger
from kernel.osdb.messages import MessagesStore

logger = get_logger("kernel.agent_framework.messages_tools")


def _threads_list(threads: Any) -> list[str]:
    if isinstance(threads, str):
        threads = [threads]
    values = [str(t).strip() for t in (threads or []) if str(t).strip()]
    if not values:
        raise ValueError("threads 不能为空（至少一个 platform:conversation_id）")
    return values


def _sender_name(message: dict[str, Any]) -> str:
    sender = message.get("sender_data") or {}
    return str(sender.get("nickname", sender.get("name", "")))


def _truncate_field(value: Any, max_chars: int) -> Any:
    """字符串字段截断：防单条记录里的 base64/超长文本整块进上下文
    （messages_query 曾因未截断把 2MB 图片内容带进 context）。"""
    if isinstance(value, str) and max_chars > 0 and len(value) > max_chars:
        return value[:max_chars] + f"…（已截断，原 {len(value)} 字符）"
    return value


def make_messages_query(store: MessagesStore, config: dict) -> Any:
    def query(sql: str, include_archive: bool = False) -> dict[str, Any]:
        """用 SQL SELECT 语句筛选消息记录。

        参数:
          sql: 只读 SELECT 语句（不支持多语句/写操作/DDL）。
            表名 messages（全部字段：seq, platform, conversation_id,
            conversation_key, conversation_type, message_type,
            remote_message_id, sender_data（JSON 文本）, content,
            llm_content, raw_segments（JSON 文本）, direction, wakeup,
            sent_to_llm, created_at（YYYY-MM-DD HH:MM:SS））。
            归档表 messages_archive 结构与 messages 完全一致。
          include_archive: true 时同时查询归档表（FROM messages 自动
            替换为主表+归档表联合）。

         用途: 复杂条件筛选（如按时间/类型/会话组合查询）。
        返回行数受 query_max_rows 限制（超限自动 LIMIT）；每个字符串
        字段受 query_max_chars 限制（截断并标注原长度，防 base64/超长
        内容撑爆上下文）。
        """
        statement = str(sql)
        if include_archive:
            statement = statement.replace(
                "FROM messages",
                "FROM (SELECT * FROM messages UNION ALL"
                " SELECT * FROM messages_archive) AS messages", 1)
        rows = store.run_readonly_query(
            statement, max_rows=int(config.get("query_max_rows", 200)))
        max_chars = int(config.get("query_max_chars", 500))
        rows = [
            {key: _truncate_field(value, max_chars)
             for key, value in row.items()}
            for row in rows
        ]
        return {"count": len(rows), "rows": rows}

    return query


def make_messages_conversations(store: MessagesStore, config: dict) -> Any:
    def conversations(threads: list[str] = None, latest: int = 0,
                      max_chars: int = 0) -> dict[str, Any]:
        """列出指定会话（threads）及每会话最新消息。

        参数:
          threads: 必填。会话列表（platform:conversation_id），如
            ["qq:group_870375059", "qq:private_123456"]。不传无法知道
            要看哪些会话，直接报错。
          latest: 每会话返回的最新消息条数（0=用配置默认）。
          max_chars: 每条 content 最大字符数，超出截断并附
            truncated_chars 提示剩余字符数（0=用配置默认）。

        用途: 快速了解指定会话最近聊了什么；**包含对方与自己发送的消息**
        （direction=out 即自己发出的，sender_name 可能为空）；content 已
        截断，完整内容用 messages_detail / messages_range /
        messages_download。**返回的消息会标记为已读；本会话中未返回的其余
        未读不受影响（仍会触发唤醒携带）。**
        """
        thread_list = _threads_list(threads)
        latest = int(latest) or int(config.get("list_latest", 5))
        max_chars = int(max_chars) or int(config.get("content_max_chars", 300))
        ordered = [store.thread_stats(thread) for thread in thread_list]
        ordered.sort(key=lambda item: -item["latest_seq"])
        result: list[dict[str, Any]] = []
        read_keys: list[str] = []
        for thread in ordered:
            messages = store.thread_messages_desc(thread["thread"],
                                                  limit=latest)
            items = []
            for message in messages:
                content = str(message.get("llm_content")
                              or message.get("content") or "")
                truncated_chars = max(0, len(content) - max_chars) \
                    if len(content) > max_chars else 0
                items.append({
                    "seq": message["seq"],
                    "created_at": message["created_at"],
                    "direction": message["direction"],
                    "sender_name": _sender_name(message),
                    "message_type": message["message_type"],
                    "content": content[:max_chars] if truncated_chars
                    else content,
                    "truncated_chars": truncated_chars,
                })
                read_keys.append(message["conversation_key"])
            result.append({
                "thread": thread["thread"],
                "count": thread["count"],
                "latest": items,
            })
        if read_keys:
            store.mark_sent_to_llm(read_keys)
        return {"count": len(result), "conversations": result}

    return conversations


def make_messages_unread(store: MessagesStore, config: dict) -> Any:
    def unread(threads: list[str] = None, x: int = 0) -> dict[str, Any]:
        """查看指定会话（threads）的未读消息。

        参数:
          threads: 必填。会话列表（platform:conversation_id）。
          x: 返回条数——从未读中按时间从晚到早返回 x 条（0=用配置默认）。
            只返回未读（direction=in 且 sent_to_llm=0），已读/死消息
            不返回。

        用途: 只关心还没处理的消息（防止死消息淹没）。取出的消息标记
        为已读；未读完的仍是未读（继续查看）。未读总数只统计最近
        unread_window_hours 小时内的（配置），更早的未读不计入。
        """
        thread_list = _threads_list(threads)
        x = int(x) or int(config.get("unread_latest", 20))
        max_chars = int(config.get("content_max_chars", 300))
        window = float(config.get("unread_window_hours", 0))
        result = []
        for thread in thread_list:
            messages = store.unread_messages(thread, limit=x,
                                             window_hours=window)
            unread_total = store.unread_count_by_thread(
                thread, window_hours=window)
            items = []
            for message in messages:
                content = str(message.get("llm_content")
                              or message.get("content") or "")
                truncated_chars = max(0, len(content) - max_chars) \
                    if len(content) > max_chars else 0
                items.append({
                    "seq": message["seq"],
                    "created_at": message["created_at"],
                    "conversation_key": message["conversation_key"],
                    "sender_name": _sender_name(message),
                    "message_type": message["message_type"],
                    "content": content[:max_chars] if truncated_chars
                    else content,
                    "truncated_chars": truncated_chars,
                })
                store.mark_sent_to_llm([message["conversation_key"]])
            result.append({
                "thread": thread,
                "unread_total": unread_total,
                "unread_returned": len(items),
                "unread_remaining": max(0, unread_total - len(items)),
                "messages": items,
            })
        return {"count": len(result), "threads": result}

    return unread


def make_messages_clear_unread(store: MessagesStore,
                                config: dict) -> Any:
    def clear_unread(thread: str, before_time: str,
                     show: int = 0) -> dict[str, Any]:
        """清除指定会话在某个时间戳之前的所有未读标记。

        参数:
          thread: 会话（platform:conversation_id），如 qq[xxx]:group_xxx。
          before_time: 时间戳 "YYYY-MM-DD HH:MM:SS"，该时间（含）之前的
            未读消息全部标记为已读。
          show: 返回被清除消息中最近多少条（默认取配置值，不足返回
            实际条数）。

        用途: 一次性清理历史积压未读（不再被唤醒携带/统计），只处理
        该时间戳之前的，之后的未读保留。返回被清除的最近几条消息
        （便于确认清了什么）+ 未展示条数。
        """
        thread_text = str(thread or "")
        before = str(before_time or "")
        show = int(show) or int(config.get("clear_show", 10))
        max_chars = int(config.get("content_max_chars", 300))
        cleared, messages = store.clear_unread_before(
            thread_text, before, show=show)
        items = []
        for message in messages:
            content = str(message.get("llm_content")
                          or message.get("content") or "")
            truncated_chars = max(0, len(content) - max_chars) \
                if len(content) > max_chars else 0
            items.append({
                "seq": message["seq"],
                "created_at": message["created_at"],
                "conversation_key": message["conversation_key"],
                "sender_name": _sender_name(message),
                "message_type": message["message_type"],
                "content": content[:max_chars] if truncated_chars
                else content,
                "truncated_chars": truncated_chars,
            })
        return {"ok": True, "thread": thread_text,
                "before_time": before,
                "cleared": cleared,
                "shown": len(items),
                "not_shown": max(0, cleared - len(items)),
                "messages": items}

    return clear_unread


def make_messages_threads(store: MessagesStore, config: dict) -> Any:
    def threads(limit: int = 0, platform: str = "") -> dict[str, Any]:
        """列出数据库中出现过的所有会话（thread = platform:conversation_id）。

        参数:
          limit: 返回的会话数量上限（0=用配置默认）。
          platform: 可选，只列该平台（qq/bilibili/...）。

        用途: 先知道现有哪些会话，再对指定会话用
        messages_conversations / messages_detail / messages_range 查看
        具体消息。只返回会话元信息（总数/最新时间），不返回消息内容。
        """
        limit = int(limit) or int(config.get("list_threads_limit", 200))
        rows = store.threads(limit=limit, platform=str(platform or ""))
        return {"count": len(rows), "threads": rows}

    return threads


def make_messages_range(store: MessagesStore, config: dict) -> Any:
    def range_query(threads: list[str] = None, start_time: str = "",
                    end_time: str = "", max_chars: int = 0,
                    limit: int = 0, include_archive: bool = False) -> dict[str, Any]:
        """按会话 + 时间区间查询消息。

        参数:
          threads: 会话级（platform:conversation_id），缺省 = "system:all"
            （所有会话，仅按时间区间）。
          start_time: 起始时间 YYYY-MM-DD HH:MM:SS（含）。
          end_time: 截止时间 YYYY-MM-DD HH:MM:SS（含）。
          max_chars: 每条 llm_content 最大字符数，超出截断并附
            truncated_chars 提示剩余字符数（0=用配置默认）。
          limit: 返回消息条数上限（0=用配置默认）。
          include_archive: true 时同时查询归档表（历史消息）。

        用途: 会话级历史/区间查询；**包含对方与自己发送的消息**
        （direction=out 即自己发出的）；查询过的消息会标记为已读（不再
        触发唤醒携带）。消息级精确读取用 messages_detail；超长内容用
        messages_download 导出为文件后阅读。
        """
        start_time = str(start_time or "").strip()
        end_time = str(end_time or "").strip()
        if not start_time and not end_time and not threads:
            raise ValueError(
                "threads/start_time/end_time 至少提供一个筛选条件")
        threads_list = list(threads) if threads else None
        max_chars = int(max_chars) or int(config.get("range_max_chars", 10000))
        limit = int(limit) or int(config.get("range_limit", 200))
        messages = store.messages_by_range(
            threads=threads_list, start_time=start_time, end_time=end_time,
            include_archive=bool(include_archive), limit=limit)
        items = []
        carried_keys = []
        for message in messages:
            content = str(message.get("llm_content")
                          or message.get("content") or "")
            truncated_chars = max(0, len(content) - max_chars) \
                if len(content) > max_chars else 0
            items.append({
                "conversation_key": message["conversation_key"],
                "created_at": message["created_at"],
                "direction": message["direction"],
                "sender_name": _sender_name(message),
                "message_type": message["message_type"],
                "content": content[:max_chars] if truncated_chars
                else content,
                "truncated_chars": truncated_chars,
            })
            carried_keys.append(message["conversation_key"])
        if carried_keys:
            store.mark_sent_to_llm(carried_keys)
        return {
            "count": len(items),
            "scope": {"threads": threads_list or "system:all",
                      "start_time": start_time, "end_time": end_time,
                      "include_archive": bool(include_archive)},
            "messages": items,
        }

    return range_query


def make_messages_detail(store: MessagesStore, config: dict) -> Any:
    def detail(conv_keys: list[str], max_chars: int = 0,
               include_archive: bool = False) -> dict[str, Any]:
        """返回指定消息（conv_keys）的完整字段记录。

         参数:
          conv_keys: 消息级 key 列表（platform:conversation_id:remote_msg_id）。
            同一 key 可能命中多条记录，满足 key 的记录全部返回。
          max_chars: 单条消息的最大字符数上限（0=用配置默认）。任何一条
            消息**序列化后**（含 content/llm_content/raw_segments 等全部
            字段，base64 图片内容也算）超过该上限 → 整体拒绝返回（防爆
            上下文），并报告是哪条消息、多少字符。
          include_archive: true 时同时查询归档表。

        用途: 精确读取消息详情（完整字段含 direction，**自己发送的
        out 消息同样可以按 key 查看**）。需要处理超长内容时改用
        messages_download 导出为文件后阅读。
        """
        keys = list(conv_keys) if conv_keys else None
        if not keys:
            raise ValueError("conv_keys 不能为空（消息级 key 列表）")
        max_chars = int(max_chars) or int(config.get("detail_max_chars", 10000))
        messages = store.messages_by_keys(
            keys, include_archive=bool(include_archive))
        for message in messages:
            # 整记录序列化大小（含 raw_segments 等全部字段）：只查 content
            # 长度会让 base64 图片内容绕过保护，撑爆上下文
            size = len(json.dumps(message, ensure_ascii=False))
            if size > max_chars:
                raise ValueError(
                    f"消息 {message['conversation_key']} 序列化后为 {size} "
                    f"字符（含全部字段），超过 max_chars={max_chars}，"
                    "拒绝返回以防爆上下文；"
                    "请调大 max_chars 或用 messages_download 导出为文件")
        if messages:
            store.mark_sent_to_llm(
                [m["conversation_key"] for m in messages])
        return {"count": len(messages), "messages": messages}

    return detail


def make_messages_download(store: MessagesStore, download_dir: str,
                           runtime_path: Any = None) -> Any:
    def download(conv_keys: list[str],
                 include_archive: bool = False) -> dict[str, Any]:
        """把指定消息（conv_keys）的全部记录导出为 jsonl 文件。

        参数:
          conv_keys: 消息级 key 列表（platform:conversation_id:remote_msg_id）。
            同一 key 命中多条（撤回/notice 同 id）时全部导出。
          include_archive: true 时同时导出归档表记录。

        用途: 处理长消息/大量消息时导出文件（每行一条完整记录、含所有
        字段），再用 workspace_read / workspace_exec 阅读分析。返回的
        ``path`` 就是当前运行时内可直接读取的路径（沙箱模式为容器内
        路径，如 /home/sandbox/download/…；宿主机模式为本机绝对路径）。

        注意: 导出的消息会被标记为已读（与其它消息查询工具一致，不再
        触发唤醒携带）。
        """
        keys = list(conv_keys) if conv_keys else None
        if not keys:
            raise ValueError("conv_keys 不能为空（消息级 key 列表）")
        messages = store.messages_by_keys(
            keys, include_archive=bool(include_archive), limit=100000)
        directory = Path(download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        filename = (f"messages_{time.strftime('%Y%m%d_%H%M%S')}"
                    f"_{len(messages)}.jsonl")
        path = directory / filename
        with path.open("w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False)
                             + "\n")
        if messages:
            store.mark_sent_to_llm(
                [m["conversation_key"] for m in messages])
        display = os.path.abspath(str(path))
        if runtime_path is not None:
            try:
                display = runtime_path(display)
            except Exception:  # noqa: BLE001 — 解析失败退回宿主绝对路径
                pass
        logger.info("messages_download wrote %d records to %s",
                    len(messages), path)
        return {"path": display, "count": len(messages)}

    return download