"""Minimal HTTP interface: SSE streaming + event injection + state/context.

Streaming path (what the frontend sees):

    LLM adapter stream  ->  Agent on_delta  ->  observer(token)
        -> StreamBus  ->  GET /api/stream (SSE)  ->  frontend

Endpoints (single port; services are separated by path):
- ``GET  /api/stream``    SSE: replay recent events then live deltas
- ``POST /api/event``     inject an event into the agent queue
                          (body: {role, content, kind?}) — 调试/前端入口
- ``POST /api/image``     前端图片上传：multipart file → 图床 → {ok, url}
                          （key 只在 kernel 侧使用，不下发浏览器）
- ``GET  /api/queue``     agent queue snapshot (events + content)
- ``GET  /api/state``     agent state / round / tools / queue length
- ``GET  /api/context``   context entries (?n=last-N)
- ``GET  /api/health``    liveness
- 消息域（需配置 messages store）：
  ``GET /api/messages`` 会话查询（?platform&conversation_id&limit&after_seq）
  ``GET /api/messages/unread`` 未读消息
  ``GET /api/messages/archive`` 归档查询（?platform&conversation_id&before&limit）
  ``POST /api/messages/archive`` 触发归档（body: {days}）
- 通知域（需配置 notices store）：
  ``POST /api/notice``  创建通知（body: {content, kind, due_at|delay_seconds|cronexpression, notification}）
  ``GET  /api/notice``  列出（?include_done=1）
  ``GET  /api/notice/{id}`` 单查
  ``DELETE /api/notice/{id}`` 删除
- ``GET  /``              minimal chat frontend
"""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import asdict
from typing import Any, Optional

from aiohttp import web

from kernel.agent_framework.agent import Agent
from kernel.agent_framework.events import EventTooLargeError
from kernel.agent_framework.image_tools import sniff_and_verify
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.skills import (delete_skill, install_skill_zip,
                                           list_skills, skill_detail)
from kernel.osdb.messages import MessagesStore
from kernel.osdb.notice import NoticeStore
from kernel.http_interface.eventbus import StreamBus

logger = get_logger("kernel.http_interface.server")


def _sse(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _token_from(request: web.Request) -> str:
    """Read the API token from header or query. SSE uses ``?token=`` because
    EventSource cannot set headers."""
    value = request.headers.get("X-Api-Token")
    if value:
        return value
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query.get("token", "")


def build_app(agent: Agent, bus: StreamBus,
              api_key: str | None = None,
              messages: Optional[MessagesStore] = None,
              notices: Optional[NoticeStore] = None,
              max_body_bytes: Optional[int] = None,
              skills: Optional[dict[str, Any]] = None) -> web.Application:
    """Build the kernel HTTP app. When ``api_key`` is set, every ``/api/*``
    request must present a matching token (constant-time compare). The
    kernel binds loopback by default; this is defence-in-depth on top of
    that (the console also enforces its own token in front).
    ``max_body_bytes`` overrides aiohttp's 1 MB request-body cap (needed for
    raw image uploads via /api/image)."""
    app_kwargs: dict[str, Any] = {}
    if max_body_bytes:
        app_kwargs["client_max_size"] = int(max_body_bytes)
    app = web.Application(**app_kwargs)

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if api_key and request.path.startswith("/api/"):
            supplied = _token_from(request)
            if not hmac.compare_digest(supplied, api_key):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    app.middlewares.append(auth_middleware)

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        try:
            async for event in bus.stream():
                # 客户端断开（刷新/切页/EventSource 关闭）是常态：写失败
                # 静默结束，不能当错误刷日志
                try:
                    await response.write(_sse(event))
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
        finally:
            try:
                await response.write_eof()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
        return response

    async def event(request: web.Request) -> web.Response:
        data = await request.json()
        content = data.get("content", "")
        # str 或 OpenAI 风格 block 列表（图片块原样保留，不得 str()）
        if not isinstance(content, (str, list)):
            return web.json_response(
                {"ok": False, "error": "content 必须是字符串或 block 列表"},
                status=400)
        try:
            agent.push_event({
                "role": str(data.get("role", "user")),
                "content": content,
                "kind": str(data.get("kind", "external")),
            })
        except EventTooLargeError as exc:
            return web.json_response({"ok": False, "error": str(exc)},
                                     status=413)
        return web.json_response({"ok": True})

    async def queue(request: web.Request) -> web.Response:
        return web.json_response([
            event.as_context_message()
            for event in agent.events.snapshot()
        ])

    async def state(request: web.Request) -> web.Response:
        return web.json_response({
            "state": agent.state.value,
            "round": agent.round_number,
            "tools": agent.tools.names(),
            "queue_length": len(agent.events),
            "imgbed": getattr(agent, "_imgbed", None) is not None,
        })

    async def image_upload(request: web.Request) -> web.Response:
        """前端图片上传：multipart → 校验 → 图床；返回 {ok, url}。"""
        client = getattr(agent, "_imgbed", None)
        if client is None:
            return web.json_response({"ok": False, "error": "图床未启用"},
                                     status=409)
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "需要 multipart/form-data"},
                status=400)
        reader = await request.multipart()
        data = b""
        filename = "upload"
        found = False
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name in ("file", "image", "smfile"):
                data = await part.read()
                filename = part.filename or "upload"
                found = True
                break
        if not found:
            return web.json_response({"ok": False, "error": "缺少 file 字段"},
                                     status=400)
        mime = sniff_and_verify(data)
        if not mime:
            return web.json_response(
                {"ok": False, "error": "文件不是可识别的图片"}, status=415)
        try:
            url = await asyncio.to_thread(client.upload, data, mime, filename)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"图床上传失败: {exc}"}, status=502)
        return web.json_response({"ok": True, "url": url})

    async def context(request: web.Request) -> web.Response:
        entries = agent.context.load()
        try:
            n = int(request.query.get("n", 0))
        except ValueError:
            n = 0
        selected = entries[-n:] if n else entries
        return web.json_response({"entries": selected, "total": len(entries)})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True,
                                  "state": agent.state.value,
                                  "alive": agent.state.value != "idle"
                                  or len(agent.events) >= 0})

    # ── 消息域 ─────────────────────────────────────────────────────────

    async def messages_unread(request: web.Request) -> web.Response:
        if messages is None:
            return web.json_response({"error": "message store not configured"},
                                     status=503)
        limit = _int_param(request, "limit", 500)
        return web.json_response({"unread": messages.pending(limit=limit)})

    async def messages_post(request: web.Request) -> web.Response:
        if messages is None:
            return web.json_response({"error": "message store not configured"},
                                     status=503)
        data = await request.json()
        try:
            row = messages.insert(
                platform=str(data.get("platform", "")),
                conversation_id=str(data.get("conversation_id", "")),
                remote_message_id=str(data.get("remote_message_id", "")),
                conversation_type=str(data.get("conversation_type", "")),
                message_type=str(data.get("message_type", "normal")),
                sender_data=data.get("sender_data"),
                content=str(data.get("content", "")),
                llm_content=str(data.get("llm_content", "")),
                raw_segments=data.get("raw_segments"),
                direction=str(data.get("direction", "in")),
                wakeup=bool(data.get("wakeup", False)),
                sent_to_llm=int(data.get("sent_to_llm", 0) or 0),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if row is None:
            return web.json_response({"ok": True, "deduped": True,
                                      "message": None})
        return web.json_response({"ok": True, "message": row}, status=201)

    async def messages_conversation(request: web.Request) -> web.Response:
        if messages is None:
            return web.json_response({"error": "message store not configured"},
                                     status=503)
        platform = request.query.get("platform", "")
        conversation_id = request.query.get("conversation_id", "")
        if not platform or not conversation_id:
            return web.json_response(
                {"error": "platform 和 conversation_id 是必填参数"},
                status=400)
        return web.json_response({
            "messages": messages.conversation(
                platform=platform,
                conversation_id=conversation_id,
                after_seq=_int_param(request, "after_seq", 0),
                limit=_int_param(request, "limit", 100),
                direction=request.query.get("direction", "all")),
        })

    async def messages_archive_get(request: web.Request) -> web.Response:
        if messages is None:
            return web.json_response({"error": "message store not configured"},
                                     status=503)
        return web.json_response({
            "messages": messages.archive_query(
                platform=request.query.get("platform", ""),
                conversation_id=request.query.get("conversation_id", ""),
                before=request.query.get("before", ""),
                limit=_int_param(request, "limit", 100)),
        })

    async def messages_archive_post(request: web.Request) -> web.Response:
        if messages is None:
            return web.json_response({"error": "message store not configured"},
                                     status=503)
        data = await request.json()
        days = int(data.get("days", 0))
        if days <= 0:
            return web.json_response({"error": "days 必须为正整数"},
                                     status=400)
        moved = messages.archive_expired(days)
        return web.json_response({"ok": True, "archived": moved})

    # ── 通知域 ─────────────────────────────────────────────────────────

    async def notice_create(request: web.Request) -> web.Response:
        if notices is None:
            return web.json_response({"error": "notice store not configured"},
                                     status=503)
        data = await request.json()
        try:
            notice_id = notices.create(
                content=str(data.get("content", "")),
                kind=str(data.get("kind", "once")),
                due_at=str(data.get("due_at", "")),
                delay_seconds=int(data.get("delay_seconds", 0)),
                cron=str(data.get("cron", "")),
                notification=str(data.get("notification", "")),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True, "id": notice_id}, status=201)

    async def notice_list(request: web.Request) -> web.Response:
        if notices is None:
            return web.json_response({"error": "notice store not configured"},
                                     status=503)
        include_done = request.query.get("include_done", "") == "1"
        items = [asdict(n) for n in notices.list(include_done=include_done)]
        return web.json_response({"notices": items})

    async def notice_get(request: web.Request) -> web.Response:
        if notices is None:
            return web.json_response({"error": "notice store not configured"},
                                     status=503)
        notice_id = int(request.match_info["id"])
        notice = notices.get(notice_id)
        if notice is None:
            return web.json_response({"error": "notice not found"}, status=404)
        return web.json_response({"notice": asdict(notice)})

    async def notice_delete(request: web.Request) -> web.Response:
        if notices is None:
            return web.json_response({"error": "notice store not configured"},
                                     status=503)
        notice_id = int(request.match_info["id"])
        if not notices.delete(notice_id):
            return web.json_response({"error": "notice not found"}, status=404)
        return web.json_response({"ok": True})

    # ── 技能域（管理界面）：列表 / 详情 / zip 安装 / 回收删除 ──────────
    # 名称优先取 SKILL.md front matter 的 name；覆盖前备份到 backup_dir，
    # 删除移入 trash_dir（都在 data/ 下，不进 agent 工作区）。
    skills_cfg = dict(skills or {})

    def _skills_ready() -> bool:
        return bool(str(skills_cfg.get("directory") or "").strip())

    def _skills_unavailable() -> web.Response:
        return web.json_response(
            {"ok": False, "error": "skills 未启用（skills.enabled=false）"},
            status=409)

    async def skills_list(request: web.Request) -> web.Response:
        if not _skills_ready():
            return _skills_unavailable()
        items = list_skills(str(skills_cfg["directory"]))
        return web.json_response({"count": len(items), "skills": items})

    async def skills_detail(request: web.Request) -> web.Response:
        if not _skills_ready():
            return _skills_unavailable()
        name = request.query.get("name", "")
        text = skill_detail(str(skills_cfg["directory"]), name,
                            int(skills_cfg.get("detail_max_chars") or 0))
        if text is None:
            return web.json_response(
                {"ok": False, "error": f"skill 不存在: {name}"}, status=404)
        return web.json_response({"ok": True, "name": name, "content": text})

    async def skills_install(request: web.Request) -> web.Response:
        if not _skills_ready():
            return _skills_unavailable()
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "需要 multipart/form-data（zip 文件）"},
                status=400)
        reader = await request.multipart()
        data = b""
        filename = "upload"
        found = False
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name in ("file", "skill", "zip"):
                data = await part.read()
                filename = part.filename or "upload"
                found = True
                break
        if not found:
            return web.json_response({"ok": False, "error": "缺少 file 字段"},
                                     status=400)
        try:
            info = install_skill_zip(
                data,
                skills_dir=str(skills_cfg["directory"]),
                backup_dir=str(skills_cfg.get("backup_dir") or ""),
                fallback_name=filename,
                max_zip_bytes=int(
                    skills_cfg.get("install_max_zip_bytes") or 0),
                max_total_bytes=int(
                    skills_cfg.get("install_max_total_bytes") or 0),
                max_files=int(skills_cfg.get("install_max_files") or 0),
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)},
                                     status=400)
        logger.info("skill installed via HTTP name=%s files=%s",
                    info.get("name"), info.get("files"))
        return web.json_response({"ok": True, **info})

    async def skills_delete(request: web.Request) -> web.Response:
        if not _skills_ready():
            return _skills_unavailable()
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "请求体需要 JSON"},
                                     status=400)
        try:
            info = delete_skill(
                str(payload.get("name") or ""),
                skills_dir=str(skills_cfg["directory"]),
                trash_dir=str(skills_cfg.get("trash_dir") or ""),
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)},
                                     status=404)
        logger.info("skill deleted via HTTP name=%s -> %s",
                    info.get("name"), info.get("trashed_to"))
        return web.json_response({"ok": True, **info})

    app.router.add_get("/api/stream", stream)
    app.router.add_post("/api/event", event)
    app.router.add_post("/api/image", image_upload)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/queue", queue)
    app.router.add_get("/api/context", context)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/messages/unread", messages_unread)
    app.router.add_get("/api/messages", messages_conversation)
    app.router.add_post("/api/messages", messages_post)
    app.router.add_get("/api/messages/archive", messages_archive_get)
    app.router.add_post("/api/messages/archive", messages_archive_post)
    app.router.add_post("/api/notice", notice_create)
    app.router.add_get("/api/notice", notice_list)
    app.router.add_get("/api/notice/{id}", notice_get)
    app.router.add_delete("/api/notice/{id}", notice_delete)
    app.router.add_get("/api/skills", skills_list)
    app.router.add_get("/api/skills/detail", skills_detail)
    app.router.add_post("/api/skills/install", skills_install)
    app.router.add_post("/api/skills/delete", skills_delete)
    return app


def _int_param(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except (TypeError, ValueError):
        return default


async def start_server(agent: Agent, bus: StreamBus,
                       host: str, port: int,
                       api_key: str | None = None,
                       messages: Optional[MessagesStore] = None,
                       notices: Optional[NoticeStore] = None,
                       max_body_bytes: Optional[int] = None,
                       skills: Optional[dict[str, Any]] = None,
                       shutdown_timeout: float = 5.0
                       ) -> tuple[object, object]:
    """Start the aiohttp server inside the running event loop."""
    app = build_app(agent, bus, api_key=api_key,
                    messages=messages, notices=notices,
                    max_body_bytes=max_body_bytes, skills=skills)
    runner = web.AppRunner(app, shutdown_timeout=shutdown_timeout)
    await runner.setup()
    site = web.TCPSite(runner, host, int(port))
    await site.start()
    return runner, site
