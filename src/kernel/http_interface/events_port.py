"""Events port — a separate HTTP service for delivering events TO the kernel.

External actors (skills / adapters / any process) POST an event to this
port; it is handed to the Beat's event queue, where kinds are dispatched in
a later node. Today kinds are undefined — the channel exists first.

Endpoints:
- ``POST /api/v1/events``   body: {kind, payload} -> beat.receive
- ``GET  /api/health``      liveness
"""

from __future__ import annotations

import hmac
from typing import Any

from aiohttp import web


def _token_from(request: web.Request) -> str:
    """Read the API token from header or query (SSE uses ?token= because
    EventSource cannot set headers)."""
    value = request.headers.get("X-Api-Token")
    if value:
        return value
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query.get("token", "")


def build_events_app(beat: Any, api_key: str = "") -> web.Application:
    """Build the events-port app. When ``api_key`` is set, every request
    must present a matching token (constant-time compare)."""
    app = web.Application()

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if (api_key and request.path != "/api/health"
                and not hmac.compare_digest(_token_from(request), api_key)):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    app.middlewares.append(auth_middleware)

    async def events(request: web.Request) -> web.Response:
        data = await request.json()
        kind = data.get("kind")
        # kind 是 JSON 对象（可携带类型与参数），便于后续按 kind 分派扩展
        if not isinstance(kind, dict) or not kind:
            return web.json_response(
                {"error": "kind 必须是非空 JSON 对象"}, status=400)
        # 投递到 beat 事件队列；kind 未定义，后续扩展分派
        beat.receive({"kind": kind, "payload": data.get("payload", {})})
        return web.json_response({"ok": True, "kind": kind})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_post("/api/v1/events", events)
    app.router.add_get("/api/health", health)
    return app


async def start_events_port(beat: Any, host: str, port: int,
                            api_key: str = "",
                            shutdown_timeout: float = 5.0
                            ) -> tuple[object, object]:
    """Start the events-port server inside the running event loop."""
    app = build_events_app(beat, api_key=api_key)
    runner = web.AppRunner(app, shutdown_timeout=shutdown_timeout)
    await runner.setup()
    site = web.TCPSite(runner, host, int(port))
    await site.start()
    return runner, site