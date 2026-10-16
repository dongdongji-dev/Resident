"""线程安全 agent 事件队列（subagent 从0重建版）。

所有事件（用户消息 / tool 结果 / 系统通知）都经此队列进入 Agent。
``wait_nonempty`` 只唤醒、不 drain——唤醒后 Agent 可先切态/睡眠/修复，
再取一个 batch。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.events")


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class Event:
    role: str
    content: str
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
        return cls(
            role=str(data.pop("role", "user")),
            content=str(data.pop("content", "")),
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