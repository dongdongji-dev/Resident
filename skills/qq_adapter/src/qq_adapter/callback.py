"""Adapter HTTP callback service: receives hook actions from the core
(POST <callback_url>) and executes them via ActionExecutor.

Deployment-agnostic: the callback port is bound locally; the core's hook
registration points callback_url at it. Actions carry the platform
instance (qq[bot]) - routing to the right WS connection is implicit
(each adapter instance owns one bot connection).

文件传输协议：core → adapter 纯 HTTP 字节转发（POST /file，二进制 +
X-Filename，Bearer token 认证）→ adapter 直接分块转发 napcat
upload_file_stream（napcat 自带缓存 file_retention，adapter 不落盘）。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from qq_adapter.logging_utils import get_logger

logger = get_logger("qq_adapter.callback")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]")


class _ActionHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server = self.server  # type: ignore[attr-defined]
        if not self._check_auth():
            return
        if self.path.rstrip("/") == "/file":
            self._receive_file()
            return
        if self.path.rstrip("/") == "/hook":
            self._execute_action()
            return
        self._respond(404, {"ok": False, "error": "unknown endpoint"})

    def do_GET(self) -> None:  # noqa: N802 (健康检查)
        server = self.server  # type: ignore[attr-defined]
        if self.path.rstrip("/") == "/status":
            executor = server.executor  # type: ignore[attr-defined]
            client = getattr(executor, "_client", None)
            ws = getattr(client, "_ws", None)
            connected = bool(ws is not None and not ws.closed)
            self._respond(200, {"ok": True, "adapter": "qq_adapter",
                                "connected": connected,
                                "platform": getattr(executor, "_platform", "")})
            return
        self._respond(404, {"ok": False, "error": "unknown endpoint"})

    def _check_auth(self) -> bool:
        """Bearer token 校验（== core api key，core 与 adapter 共享）。"""
        server = self.server  # type: ignore[attr-defined]
        expected = getattr(server, "auth_token", "")
        if not expected:
            self._respond(503, {"ok": False, "error": "auth not configured"})
            return False
        auth = self.headers.get("Authorization", "")
        provided = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        if not provided:
            # 兼容 X-Api-Token 头（agent curl 常用）
            provided = self.headers.get("X-Api-Token", "")
        if provided != expected:
            self._respond(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _receive_file(self) -> None:
        """文件流式转发：边收边分块转发 napcat upload_file_stream
        （内存 O(chunk)；napcat 缓存 file_retention 自管）。sha 由
        core 先行计算随 X-Sha256 头传入。"""
        import asyncio
        import hashlib
        import uuid
        server = self.server  # type: ignore[attr-defined]
        executor = server.executor  # type: ignore[attr-defined]
        max_bytes: int = server.transfer_max_bytes  # type: ignore[attr-defined]
        raw_name = self.headers.get("X-Filename", "")
        name = _SAFE_NAME.sub("_", raw_name)[:120] or "file.bin"
        sha = str(self.headers.get("X-Sha256", "")).strip()
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_bytes:
            self._respond(413, {"ok": False,
                                "error": f"文件大小超限（上限 {max_bytes}）"})
            return
        if not sha:
            self._respond(400, {"ok": False, "error": "缺少 X-Sha256 头"})
            return
        loop = getattr(executor, "_loop", None)
        if loop is None:
            self._respond(503, {"ok": False, "error": "executor 未绑定循环"})
            return
        stream_id = str(uuid.uuid4())
        chunk_size = 256 * 1024
        total = max(1, (length + chunk_size - 1) // chunk_size)
        index = 0
        received = 0
        try:
            while received < length:
                chunk = self.rfile.read(min(chunk_size, length - received))
                if not chunk:
                    break
                future = asyncio.run_coroutine_threadsafe(
                    executor.upload_chunk(stream_id, chunk, index, total,
                                          length, sha, name), loop)
                future.result(timeout=120)
                received += len(chunk)
                index += 1
            if received != length:
                raise RuntimeError(
                    f"body 截断: 期望 {length} 实际 {received}")
            future = asyncio.run_coroutine_threadsafe(
                executor.complete_upload(stream_id, total, length, sha,
                                         name), loop)
            file_path = future.result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            logger.error("file forward failed name=%s: %s", name, exc)
            self._respond(502, {"ok": False,
                                "error": f"转发 napcat 失败: {exc}"})
            return
        logger.info("file forwarded name=%s size=%d path=%s",
                    name, received, file_path)
        self._respond(200, {"ok": True, "path": file_path,
                            "size": received})

    def _execute_action(self) -> None:
        executor = self.server.executor  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            import json as _json
            payload = _json.loads(raw.decode("utf-8"))
        except ValueError:
            self._respond(400, {"ok": False, "error": "invalid json"})
            return
        try:
            from qq_adapter.xml_protocol import parse_action
            action = parse_action(payload)
        except Exception as exc:  # noqa: BLE001
            self._respond(400, {"ok": False, "error": f"action 解析失败: {exc}"})
            return
        result = executor.execute_sync(action)
        logger.info("hook action executed platform=%s ok=%s",
                    action.get("platform"), result.get("ok"))
        self._respond(200, result)

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


class CallbackServer:
    """Bind ActionExecutor + file forwarder to a local HTTP endpoint."""

    def __init__(self, executor: Any,
                 transfer_max_bytes: int = 500 * 1024 * 1024,
                 auth_token: str = "",
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self._executor = executor
        self._server = ThreadingHTTPServer((host, port), _ActionHandler)
        self._server.executor = executor  # type: ignore[attr-defined]
        self._server.auth_token = auth_token  # type: ignore[attr-defined]
        self._server.transfer_max_bytes = int(transfer_max_bytes)  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name="napcat-adapter-callback")
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(2)
