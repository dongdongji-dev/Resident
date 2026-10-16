"""Provider 协议归一化（subagent 从0重建版）。

- ``sanitize_tool_messages``：每个 assistant tool-call 恰好一个紧随的
  ``role=tool`` 响应；缺失补占位、孤儿/重复丢弃、tool 消息不当首个非 system。
- ``merge_user_events``：多条 user 消息合成一条。
- ``append_trailing_empty_user``：后端拒收以 ``role=tool`` 结尾的请求时，
  追加一条临时空 user。
"""

from __future__ import annotations

from kernel.agent_framework.events import Event


def sanitize_tool_messages(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    pending: list[str] = []
    responded: set[str] = set()

    def close_pending() -> None:
        for call_id in pending:
            normalized.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": "(missing tool response; call may be retried)",
            })
        pending.clear()

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
                continue
            normalized.append(message)
            responded.add(call_id)
            pending = [item for item in pending if item != call_id]
            continue
        if pending:
            close_pending()
        normalized.append(message)

    close_pending()
    return normalized


def make_merged_user(users: list[Event], separator: str,
                     notice: str) -> Event:
    head = users[0]
    body = f"{separator}".join(e.content for e in users).strip(separator).strip()
    if notice:
        content = f"{notice}{separator}{body}" if body else notice
    else:
        content = body
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
    if enabled and messages and messages[-1].get("role") == "tool":
        return [*messages, {"role": "user", "content": ""}]
    return messages