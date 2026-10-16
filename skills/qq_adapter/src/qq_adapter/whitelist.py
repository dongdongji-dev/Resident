"""会话白名单（V2 whitelist 双层语义）：
- input 侧：事件所属会话不在白名单 → 直接丢弃（不入库）
- 执行侧：发送动作目标会话不在白名单 → 拒绝（PermissionError）

enabled=false 表示该维度不限制（全部放行）；enabled=true 时仅列表内
允许。会话 id 按群号 / QQ 号（不带 group_/private_ 前缀）。"""

from __future__ import annotations

from typing import Any

from qq_adapter.logging_utils import get_logger

from .config import WhitelistConfig

logger = get_logger("qq_adapter.whitelist")


class Whitelist:
    def __init__(self, config: WhitelistConfig) -> None:
        self._config = config

    # ── input 侧 ────────────────────────────────────────────────────────

    def allow_inbound(self, event: dict[str, Any]) -> bool:
        """事件是否允许入库：按消息/通知的会话维度判定。"""
        message_type = event.get("message_type")
        if message_type == "group" or event.get("group_id"):
            return self._allow_group(str(event.get("group_id", "")))
        if message_type == "private" or event.get("user_id"):
            return self._allow_private(str(event.get("user_id", "")))
        # 无会话信息的事件（meta 等）放行（不入库事件在 handler 层丢弃）
        return True

    def _allow_group(self, group_id: str) -> bool:
        if not self._config.group_enabled:
            return True
        allowed = group_id in self._config.group_ids
        if not allowed:
            logger.info("inbound dropped: group %s not whitelisted", group_id)
        return allowed

    def _allow_private(self, user_id: str) -> bool:
        if not self._config.private_enabled:
            return True
        allowed = user_id in self._config.private_ids
        if not allowed:
            logger.info("inbound dropped: private %s not whitelisted", user_id)
        return allowed

    # ── 执行侧 ──────────────────────────────────────────────────────────

    def guard_outbound(self, conversation_type: str,
                       conversation_id: str) -> None:
        """发送动作校验：目标会话不在白名单 → PermissionError。
        conversation_id 可为 group_200000001 / private_1000000002 或裸 id。"""
        raw = str(conversation_id)
        ctype = str(conversation_type)
        if ctype == "group" or raw.startswith("group_"):
            gid = raw[len("group_"):] if raw.startswith("group_") else raw
            if self._config.group_enabled and gid not in self._config.group_ids:
                raise PermissionError(
                    f"目标群 {gid} 不在白名单，拒绝执行")
            return
        if ctype == "private" or raw.startswith("private_"):
            uid = raw[len("private_"):] if raw.startswith("private_") else raw
            if self._config.private_enabled \
                    and uid not in self._config.private_ids:
                raise PermissionError(
                    f"目标私聊 {uid} 不在白名单，拒绝执行")
            return
        raise PermissionError(f"未知会话类型 {ctype!r}，拒绝执行")
