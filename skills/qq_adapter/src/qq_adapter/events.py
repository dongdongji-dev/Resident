"""Event dispatch registry: (post_type, sub_type, notice_type) -> handler.

One branch per handler - registered with ``@register_event`` and developed
+ tested independently. Lookup order: exact (post_type, sub_type,
notice_type) -> (post_type, sub_type, "") -> (post_type, "", "") ->
default (drop with log). Event-level content (poke/recall/requests) is
built here by parsing raw_info; segment-based messages go through
``assemble_llm_content``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from qq_adapter.logging_utils import get_logger

from .message import MessageDraft, MessageSubmitter

logger = get_logger("qq_adapter.events")

EventHandler = Callable[[dict[str, Any], "EventContext"], "MessageDraft | None"]


class _Registry(dict[tuple[str, str, str], EventHandler]):
    pass


EVENT_HANDLERS: _Registry = _Registry()


def register_event(post_type: str, sub_type: str = "",
                   notice_type: str = "") -> Callable[[EventHandler], EventHandler]:
    """Register one event branch. ``sub_type`` / ``notice_type`` may be
    left empty to match any value at that level."""

    def wrap(fn: EventHandler) -> EventHandler:
        EVENT_HANDLERS[(post_type, sub_type, notice_type)] = fn
        return fn

    return wrap


@dataclass(slots=True)
class EventContext:
    """Per-adapter runtime context handed to every handler."""

    submitter: MessageSubmitter
    self_id: str = ""          # bot QQ（登录信息拉取）
    platform: str = ""         # qq[10001]
    names: dict[str, str] = field(default_factory=dict)  # uid -> 展示名缓存
    config_wakeup_on_mention: bool = True
    media_download_timeouts: dict = field(default_factory=dict)
    # 入站媒体下载按类型超时（秒），见 config.OneBotConfig / media.DEFAULT_TIMEOUTS
    media_download_max_mb: float = 50.0
    # 自动下载大小上限（MB）：超过就不下、消息给直链、并发 notice（2026-10-16）
    current_conversation_id: str = ""  # 当前事件所属会话（渲染上下文）
    client: Any = None                 # OneBotClient（file url 补拉等）
    loop: Any = None                   # 主事件循环（后台任务调度）
    attention: Any = None              # AttentionStore（注意力群，get() 取当前）

    def name_of(self, user_id: Any) -> str:
        """展示名：群名片 → 昵称 → QQ 号（V2 回退链）。"""
        uid = str(user_id or "")
        if not uid:
            return ""
        name = self.names.get(uid)
        return name if name else uid

    def submit(self, draft: "MessageDraft") -> Optional[dict[str, Any]]:
        return self.submitter.submit(draft)


async def dispatch_event(event: dict[str, Any], ctx: EventContext):
    """Route one raw OneBot event to its registered branch.

    Candidate keys tried in order (exact -> wildcard):
      (post_type, sub_type, notice_type)
      (post_type, sub_type, "")
      (post_type, message_type, "")   # message 事件按 message_type 匹配
      (post_type, notice_type, "")    # notice 事件按 notice_type 匹配
      (post_type, "", "")
    Unhandled events are dropped with a debug log (information, not errors).
    2026-08-25：异步化——消息类 handler（_handle_message）要前置下载媒体/
    拉取转发，改为 async；其余 handler 仍是同步函数，统一 await 兼容。"""
    import inspect
    post_type = str(event.get("post_type", ""))
    sub_type = str(event.get("sub_type", ""))
    notice_type = str(event.get("notice_type", ""))
    message_type = str(event.get("message_type", ""))
    request_type = str(event.get("request_type", ""))
    logger.info("event arrived post=%s notice=%s sub=%s msg_type=%s req=%s",
                post_type, notice_type, sub_type, message_type, request_type)
    for key in ((post_type, sub_type, notice_type),
                (post_type, sub_type, ""),
                (post_type, message_type, ""),
                (post_type, notice_type, ""),
                (post_type, request_type, ""),
                (post_type, "", notice_type),
                (post_type, "", "")):
        handler = EVENT_HANDLERS.get(key)
        if handler is not None:
            try:
                draft = handler(event, ctx)
                if inspect.isawaitable(draft):
                    draft = await draft
            except Exception:  # noqa: BLE001
                logger.exception("event handler failed post_type=%s "
                                 "sub_type=%s notice_type=%s",
                                 post_type, sub_type, notice_type)
                return None
            if draft is not None:
                return ctx.submitter.submit(draft)
            return None
    logger.debug("unhandled event dropped post_type=%s sub_type=%s "
                 "notice_type=%s", post_type, sub_type, notice_type)
    return None
