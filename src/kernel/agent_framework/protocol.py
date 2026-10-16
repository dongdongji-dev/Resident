"""Provider protocol normalization.

Pure helpers shared by the ReAct loop:
- ``sanitize_tool_messages``: every assistant tool-call has exactly one
  following ``role=tool`` response; missing responses get a placeholder,
  orphan/duplicate tool responses are dropped, a tool message never becomes
  the first non-system message.
- ``merge_user_events``: collapse several user messages into one, joined by
  a config separator + a reminder notice.
- ``append_trailing_empty_user``: some backends reject requests that end in
  ``role=tool``; append a temporary empty user message when enabled.
"""

from __future__ import annotations

from typing import Iterable

from kernel.agent_framework.content import merge_contents
from kernel.agent_framework.events import Event

# 静默协议：模型"无话可说"时输出该标记（或空输出）。Agent 识别后
# 不把内容计入 model_output 自说自话（概率衰减由 agent 线程按
# "无工具调用"处理，silence 是其子集）。
SILENCE_MARKER = "[|silence_output|]"


def is_silence_output(text: str) -> bool:
    """True when a final assistant output means "stay silent": the silence
    marker appears anywhere (the model may attach a short confirmation
    before it) or the output is completely empty."""
    stripped = str(text or "").strip()
    return stripped == "" or SILENCE_MARKER in stripped


def sanitize_tool_messages(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    pending: list[str] = []
    responded: set[str] = set()

    def close_pending() -> None:
        nonlocal pending
        for call_id in pending:
            normalized.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": "(missing tool response; call may be retried)",
            })
        pending = []

    for original in messages:
        message = dict(original)
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            close_pending()
            calls = message.get("tool_calls") or []
            pending = [str(call.get("id"))
                       for call in calls
                       if isinstance(call, dict) and call.get("id")]
            responded = set()
            normalized.append(message)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not pending or call_id not in pending or call_id in responded:
                continue  # orphan / duplicate
            normalized.append(message)
            responded.add(call_id)
            pending = [item for item in pending if item != call_id]
            continue
        if pending:
            close_pending()
        normalized.append(message)

    close_pending()
    return normalized


def make_merged_user(users: list[Event], separator: str, notice: str) -> Event:
    """One merged user event from several: join contents with ``separator``
    and prepend a human-readable reminder that the messages were batched.
    Batches containing image blocks keep block form (texts + images in
    order); pure-text batches keep the historical string join."""
    head = users[0]
    content = merge_contents([event.content for event in users],
                             separator, notice)
    return Event(
        role="user",
        content=content,
        event_id=head.event_id,
        kind=head.kind,
        source=head.source,
        created_at=head.created_at,
        metadata=dict(head.metadata),
    )


def merge_user_events(events: list[Event], enabled: bool,
                      separator: str, notice: str) -> list[Event]:
    """Collapse all user-role events into one at the first user position.
    Non-user events keep their relative order."""
    users = [e for e in events if e.role == "user"]
    if not enabled or len(users) < 2:
        return events
    merged = make_merged_user(users, separator, notice)
    result: list[Event] = []
    inserted = False
    for event in events:
        if event.role == "user":
            if not inserted:
                result.append(merged)
                inserted = True
            continue
        result.append(event)
    return result


def append_trailing_empty_user(
    messages: list[dict], enabled: bool
) -> list[dict]:
    """Add a temporary empty user message when the provider rejects a
    ``role=tool`` tail."""
    if enabled and messages and messages[-1].get("role") == "tool":
        return [*messages, {"role": "user", "content": ""}]
    return messages
