"""Inbound message assembly: raw onebot segments -> MessageDraft.

Every inbound event eventually becomes a MessageDraft (the adapter-side
projection of the core ``messages`` row) and is submitted via
``POST /api/messages`` (dedup + identity upsert happen core-side).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from qq_adapter.logging_utils import get_logger

from .segments import render_segment

logger = get_logger("qq_adapter.message")


@dataclass(slots=True)
class MessageDraft:
    platform: str                 # qq[10001]
    conversation_id: str          # group_200000001 / private_1000000002
    conversation_type: str        # group / private / ...
    remote_message_id: str        # 平台消息 id（notice 类可空）
    message_type: str = "normal"  # normal / poke / recall / request / ...
    sender: dict[str, Any] = field(default_factory=dict)
    content: str = ""             # 原始格式化文本
    llm_content: str = ""         # 模型可见内容（渲染后）
    raw_segments: list[Any] = field(default_factory=list)
    wakeup: bool = False
    direction: str = "in"         # in（收到）/ out（自己发出）
    created_at: str = ""          # 平台侧时间（本地 YYYY-MM-DD HH:MM:SS）


def assemble_llm_content(segments: list[dict[str, Any]], ctx: Any) -> str:
    """Order-preserving assembly: every segment rendered in place."""
    parts = []
    for seg in segments or []:
        parts.append(render_segment(seg, ctx))
    return "".join(parts)


def _local_now_iso(unix_seconds: Any) -> str:
    import time
    try:
        stamp = int(unix_seconds)
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


class MessageSubmitter:
    """POSTs MessageDrafts to the core message layer (HTTP)."""

    def __init__(self, core_url: str, api_key: str,
                 platform: str) -> None:
        self._core_url = str(core_url).rstrip("/")
        self._api_key = str(api_key)
        self._platform = str(platform)

    def report_media_description(self, url: str,
                                 description: str) -> Optional[dict[str, Any]]:
        """上报媒体识别/转写结果到 core（media_cache 共享 + 消息回填）。"""
        body = json.dumps({"url": url, "description": description},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._core_url}/api/media/description", data=body,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception as exc:  # noqa: BLE001
            logger.error("media description report failed: %s", exc)
            return None

    def get_message(self, conversation_key: str) -> Optional[dict[str, Any]]:
        """按 key 查 core 消息（转写回填 llm_content 用）。"""
        import urllib.parse
        request = urllib.request.Request(
            f"{self._core_url}/api/messages/"
            f"{urllib.parse.quote(conversation_key)}",
            headers={"X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="replace")
            result = json.loads(raw) if raw.strip() else {}
            return result.get("message") or None
        except Exception:  # noqa: BLE001
            return None

    def get_by_remote(self, platform: str,
                      remote_message_id: str) -> Optional[dict[str, Any]]:
        """按 (platform, remote_message_id) 查 core 消息。

        ⚠️ core 没有这个端点：`GET /api/messages/by-remote` 2026-09-24 实测 404
        （列表端点只支持 platform+conversation_id）。引用的判断已改走
        `qq_adapter.outbound` 内存登记（见 handlers._is_bot_message）。
        这里保留方法是为了不破坏接口；调用前先确认 core 是否已补该路由。"""
        import urllib.parse
        query = urllib.parse.urlencode({
            "platform": platform, "remote_message_id": remote_message_id})
        request = urllib.request.Request(
            f"{self._core_url}/api/messages/by-remote?{query}",
            headers={"X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="replace")
            result = json.loads(raw) if raw.strip() else {}
            return result.get("message") or None
        except Exception:  # noqa: BLE001
            return None

    def patch_message(self, conversation_key: str,
                      fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        """更新 core 消息（PATCH 白名单字段）——媒体 url 补拉等用。"""
        import urllib.parse
        body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._core_url}/api/messages/"
            f"{urllib.parse.quote(conversation_key)}",
            data=body, method="PATCH",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception as exc:  # noqa: BLE001
            logger.error("patch failed key=%s: %s", conversation_key, exc)
            return None

    def update_platform(self, platform: str) -> None:
        """连接后按 get_login_info 的 self_id 更新平台实例标识。"""
        self._platform = str(platform)

    def create_notice(self, content: str,
                      notification: str = "") -> Optional[dict[str, Any]]:
        """在 core 创建一条**立即触发的 once 通知**（notice 通道）。

        适配器主动自我提醒走这里（如注意力状态转移）——notice 由 beat
        在到点时推给 agent（呈现为一条通知），**不进 messages
        库**（2026-10-12 约定明确：这类提示应是 notice，不是消息）。
        """
        payload = {"content": content, "kind": "once"}
        if notification:
            payload["notification"] = notification
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._core_url}/api/notice", data=body,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
            result = json.loads(raw) if raw.strip() else {}
            logger.info("notice created id=%s", result.get("id"))
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("notice create failed: %s", exc)
            return None

    def submit(self, draft: MessageDraft) -> Optional[dict[str, Any]]:
        # 无会话的 input（如部分 notice）统一补到 bot 自身会话
        if not draft.conversation_id:
            draft.conversation_id = "qq_self"
            draft.conversation_type = draft.conversation_type or "self"
        payload = {
            "platform": draft.platform or self._platform,
            "conversation_id": draft.conversation_id,
            "conversation_type": draft.conversation_type,
            "remote_message_id": draft.remote_message_id,
            "message_type": draft.message_type,
            "sender_data": draft.sender,
            "content": draft.llm_content or draft.content,
            "wakeup": draft.wakeup,
            "direction": draft.direction,
        }
        # raw_segments 入库（request 事件含审批 flag 等协议凭据，core 已
        # 验证支持存储；2026-09-06 群申请审批因缺 flag 失败的修复）
        if draft.raw_segments:
            payload["raw_segments"] = draft.raw_segments
        if draft.created_at:
            payload["created_at"] = draft.created_at
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._core_url}/api/messages", data=body,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
            result = json.loads(raw) if raw.strip() else {}
            logger.info("submitted %s key=%s deduped=%s",
                        draft.message_type, draft.remote_message_id,
                        result.get("deduped"))
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("submit failed conv=%s msg=%s: %s",
                         draft.conversation_id, draft.remote_message_id, exc)
            return None
