"""Adapter media file server: serves files under the media root over HTTP
for large-file sending (napcat fetches the URL - works across servers).

Security: only files under ``files_root`` are served (prefix guard +
symlink resolve); read-only, no listing.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("qq_adapter.fileserver")


class _MediaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        server = self.server  # type: ignore[attr-defined]
        files_root: Path = server.files_root  # type: ignore[attr-defined]
        relative = self.path.lstrip("/")
        if not relative.startswith("media/"):
            self._respond(404, b"not found")
            return
        target = (files_root / relative[len("media/"):]).resolve()
        if target != files_root and files_root not in target.parents:
            self._respond(403, b"forbidden")
            return
        if not target.is_file():
            self._respond(404, b"not found")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._respond(500, b"read failed")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


class MediaFileServer:
    """Serves media files for large-file sending (napcat fetches by URL)."""

    def __init__(self, files_root: str, host: str = "0.0.0.0",
                 port: int = 0) -> None:
        self._files_root = Path(files_root)
        self._server = ThreadingHTTPServer((host, port), _MediaHandler)
        self._server.files_root = self._files_root  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name="napcat-media-fileserver")
        self._thread.start()
        logger.info("media file server listening on 0.0.0.0:%d", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(2)
