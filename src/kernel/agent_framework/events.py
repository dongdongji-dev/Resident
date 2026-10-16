"""Thread-safe agent event queue.

All events (messages, tool results, system notices) enter the Agent only
through this queue. ``wait_nonempty`` wakes the loop WITHOUT draining so the
Agent can switch state / sleep / repair before taking a batched drain.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.timeutil import now_str as _now

logger = get_logger("kernel.agent_framework.events")


class EventTooLargeError(RuntimeError):
    """Image-carrying event rejected by the context byte budget (admission
    happens before it enters the queue; callers report the error instead of
    silently dropping)."""


@dataclass(slots=True)
class Event:
    role: str
    content: Any  # str 或 OpenAI 风格 block 列表（见 content.py）
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "external"
    source: str = "runtime"
    created_at: str = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: "Event | dict[str, Any]") -> "Event":
        if isinstance(value, Event):
            return value
        if not isinstance(value, dict):
            raise TypeError("event must be Event or mapping")
        data = dict(value)
        raw_content = data.pop("content", "")
        if isinstance(raw_content, list):
            content: Any = [dict(item) if isinstance(item, dict) else item
                            for item in raw_content]
        elif raw_content is None:
            content = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = str(raw_content)
        return cls(
            role=str(data.pop("role", "user")),
            content=content,
            event_id=str(data.pop("event_id", uuid.uuid4())),
            kind=str(data.pop("kind", "external")),
            source=str(data.pop("source", "runtime")),
            created_at=str(data.pop("created_at", _now())),
            metadata=dict(data.pop("metadata", data)),
        )

    def as_context_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "created_at": self.created_at,
        }
        message.update(self.metadata)
        return message


class EventQueue:
    def __init__(self) -> None:
        self._items: deque[Event] = deque()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._closed = False

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._wake = asyncio.Event()
        with self._lock:
            has_items = bool(self._items)
        if has_items:
            self._wake.set()

    def push(self, value: Event | dict[str, Any]) -> Event:
        event = Event.from_value(value)
        with self._lock:
            if self._closed:
                raise RuntimeError("event queue is closed")
            self._items.append(event)
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)
        return event

    def drain(self) -> list[Event]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def snapshot(self) -> list[Event]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    async def wait_nonempty(self) -> None:
        """Block until the queue has at least one event; does NOT drain.

        This is the idle wait: when it returns, the caller owns the
        transition to STREAMING and may sleep before the actual drain so a
        burst of events lands in one batch.
        """
        if self._wake is None:
            raise RuntimeError("event queue is not attached to an event loop")
        while True:
            with self._lock:
                if self._items:
                    return
                if self._closed:
                    return
            await self._wake.wait()
            self._wake.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass
