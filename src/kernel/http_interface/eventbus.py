"""Streaming event bus — Agent observer events fanned out to SSE clients.

The Agent's observer callable receives dict events (``token`` /
``context_appended`` / ``state_changed`` / ``round_started`` ...). A bounded
history lets subscribers that connect late replay recent events before
following live deltas (so a frontend refresh still shows what happened).

Thread safety: ``publish`` may be called from the agent loop or HTTP handlers
(same event loop) or from worker threads; delivery is scheduled through
``loop.call_soon_threadsafe`` per subscriber.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, AsyncIterator


def _deliver(queue: "asyncio.Queue[dict[str, Any]]",
             event: dict[str, Any]) -> None:
    """Enqueue one event for a subscriber WITHOUT ever raising: when the
    bounded queue is full (slow or half-dead SSE client) drop the OLDEST
    event and keep the newest — context.jsonl is the single source of truth,
    the stream is only an animation layer. A raw put_nowait here would raise
    QueueFull inside the event loop callback and spam ERROR logs."""
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


_CLOSE_SENTINEL: dict[str, Any] = {"type": "_bus_closed"}


class StreamBus:
    def __init__(self, history_limit: int = 200) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._subs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """End every live SSE stream promptly (shutdown/reboot path).

        Without this, aiohttp waits ``shutdown_timeout`` (x2, measured) for
        long-lived ``/api/stream`` connections during ``runner.cleanup()``.
        """
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            self._schedule(sub, dict(_CLOSE_SENTINEL))

    def publish(self, event: dict[str, Any]) -> None:
        """Fan out one observer event to every live subscriber (and history)."""
        if not isinstance(event, dict):
            return
        with self._lock:
            self._history.append(dict(event))
            subs = list(self._subs)
        for sub in subs:
            self._schedule(sub, dict(event))

    def _schedule(self, sub: dict[str, Any], event: dict[str, Any]) -> None:
        loop = sub["loop"]
        queue = sub["queue"]
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(_deliver, queue, event)
        except RuntimeError:
            pass

    def _subscribe(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with self._lock:
            history = list(self._history)
        loop = asyncio.get_running_loop()
        sub = {"loop": loop, "queue": asyncio.Queue(maxsize=500)}
        with self._lock:
            self._subs.append(sub)
        return history, sub

    def _unsubscribe(self, sub: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    async def stream(self, heartbeat: float = 15.0) -> AsyncIterator[dict[str, Any]]:
        """Async iterator for one SSE client: replay history, then live
        deltas; yields a ``{"type": "heartbeat"}`` every ``heartbeat`` s so
        proxies do not drop the idle connection. Ends promptly when the bus
        is closed (process shutdown/reboot)."""
        history, sub = self._subscribe()
        queue: asyncio.Queue[dict[str, Any]] = sub["queue"]
        try:
            if self._closed:
                return
            for event in history:
                # 回放的历史事件加 _replay 标记：前端页面加载时已通过
                # /api/context 全量拉取过消息，回放只用于补实时前的状态
                # （state_changed 等），消息类（context_appended/round_started）
                # 由前端忽略，避免整段消息重复渲染。
                yield {**event, "_replay": True}
            while True:
                if self._closed:
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                if event.get("type") == _CLOSE_SENTINEL["type"]:
                    return
                yield event
        finally:
            self._unsubscribe(sub)
