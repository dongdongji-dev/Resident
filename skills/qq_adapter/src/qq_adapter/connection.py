"""OneBot11 forward WebSocket client (connection skeleton).

Connects to napcat, authenticates with the access token, forwards every
event to ``on_event`` (which dispatches through the event registry), and
exposes ``api()`` for OneBot11 actions. Reconnect uses exponential backoff
(V2 parameters); the connection is deployment-agnostic (container / local
binary / remote VPS are all just a ws URL).

Skeleton stage: connect + receive + dispatch wired; api() echo matching
lands with the first outbound branch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger("qq_adapter.connection")

RECONNECT_BACKOFF = (1, 2, 4, 8, 16, 30, 60)


class OneBotClient:
    def __init__(self, config, on_event: Callable[[dict[str, Any]], Any],
                 on_connected: Optional[Callable[["OneBotClient"], Any]] = None) -> None:
        self._config = config
        self._on_event = on_event
        self._on_connected = on_connected
        self._session: Optional[Any] = None
        self._ws: Optional[Any] = None
        self._running = False
        self._pending: dict[str, asyncio.Future] = {}
        # 事件处理任务集合（不阻塞读循环）＋ 同会话保序锁
        self._event_tasks: set[asyncio.Task] = set()
        self._conv_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self._running = True
        attempt = 0
        while self._running:
            try:
                await self._connect_once()
                attempt = 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("onebot connection failed: %s", exc)
                if not self._running:
                    break
                delay = self._config.reconnect_backoff[
                    min(attempt, len(self._config.reconnect_backoff) - 1)]
                attempt += 1
                logger.info("reconnecting in %ss (attempt %d)", delay, attempt)
                await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        try:
            import aiohttp
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError("aiohttp required for onebot ws: pip install aiohttp") from exc
        url = str(self._config.ws_url)
        if self._config.access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._config.access_token}"
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.ws_connect(
                    url, heartbeat=self._config.heartbeat_seconds) as ws:
                self._ws = ws
                logger.info("onebot ws connected: %s", self._config.ws_url)
                # 先启动消息读取任务，再执行连接回调（回调里的 api() 调用
                # 依赖读取任务消费 echo 响应——顺序颠倒会死锁）
                read_task = asyncio.create_task(self._read_loop(ws))
                if self._on_connected is not None:
                    try:
                        await self._on_connected(self)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_connected callback failed")
                await read_task
        finally:
            await self._session.close()
            self._session = None
            self._ws = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("onebot disconnected"))
            self._pending.clear()

    async def _read_loop(self, ws: Any) -> None:
        """Consume WS messages: echo responses resolve pending api()
        futures; everything else is dispatched as an inbound event."""
        import aiohttp
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                continue
            echo = payload.get("echo")
            if echo and echo in self._pending:
                future = self._pending.pop(echo)
                if not future.done():
                    future.set_result(payload)
                continue
            self._dispatch(payload)

    def _dispatch(self, payload: dict[str, Any]) -> None:
        """把事件处理派发到独立 task——**绝不能让事件处理阻塞 _read_loop**。

        2026-10-16 修复（症状：私聊/群文件不落盘；发文件后消息推送晚 ~50s）：
        旧实现是 `await self._on_event(payload)`，即读循环亲自等事件处理跑完；
        而事件处理里会调用 `api()`（get_private_file_url / get_group_file_url /
        fetch_ptt_text / get_forward_msg）——echo 响应只能由 _read_loop 消费，
        于是自己把自己锁死：必然等到 `api_timeout_seconds` 超时（DEFAULT 15s，
        file 段还重试 3 次 ⇒ ~50s）才往下走。图片/语音无需 api 补拉所以一直正常。
        实测两种写法（`tmp/test_ws_deadlock.py`）：阻塞版 6.01s 超时 / create_task
        版 0.01s 成功。⇒ 读循环只管收发与 echo 匹配，事件处理另起 task。
        同会话（post_type+message_type+会话 id）用锁串行以保序，不同会话并行。
        """
        key = "{post}:{mtype}:{cid}".format(
            post=payload.get("post_type") or "",
            mtype=payload.get("message_type") or "",
            cid=payload.get("group_id") or payload.get("user_id") or "")
        lock = self._conv_locks.setdefault(key, asyncio.Lock())

        async def _run() -> None:
            async with lock:
                try:
                    await self._on_event(payload)
                except Exception:  # noqa: BLE001
                    logger.exception("event handler failed")

        task = asyncio.create_task(_run())
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def get_login_info(self) -> dict[str, Any]:
        """bot 自身信息（self_id/nickname）——平台实例标识来源。"""
        result = await self.api("get_login_info")
        return result.get("data") or {}

    async def api(self, action: str,
                  params: dict[str, Any] | None = None) -> dict[str, Any]:
        """OneBot11 action call with echo matching (15s timeout default)."""
        ws = getattr(self, "_ws", None)
        if ws is None or ws.closed:
            raise ConnectionError("onebot not connected")
        echo = f"api-{uuid.uuid4().hex[:12]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        await ws.send_json({
            "action": action, "params": params or {}, "echo": echo})
        try:
            return await asyncio.wait_for(
                future, timeout=self._config.api_timeout_seconds)
        except asyncio.TimeoutError as exc:  # noqa: BLE001
            self._pending.pop(echo, None)
            raise TimeoutError(f"onebot api timeout: {action}") from exc

    async def stop(self) -> None:
        self._running = False
        if self._session is not None:
            await self._session.close()
