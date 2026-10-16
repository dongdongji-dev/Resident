"""Console — control plane (supervisor) + single-port frontend.

A standalone process (independent from the kernel) that:

- keeps the kernel alive: one-click start / stop / restart; the kernel is
  spawned as a subprocess whose stdout/stderr land in a persistent log file
  ``data/logs/kernel.log`` (size-capped, rotated to ``.archive``);
- serves the SPA frontend (Console / Chat / Log views) from
  ``kernel/console/frontend/index.html``;
- exposes ONE HTTP port; every service is a separate path:
    /api/status          this process: kernel running/pid/log file
    /api/control/start   one-click start (ensures kernel alive)
    /api/control/stop    graceful stop
    /api/control/restart
    /api/logs            incremental tail of the persisted kernel log
    /api/* (rest)        streaming reverse proxy to the kernel's
                         http_interface (chat, SSE, queue, context, event…)

Run::

    python -m kernel.console --config config/console.yaml
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import time
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# Project layout: src/kernel/console/app.py -> <repo root>
ROOT = Path(__file__).resolve().parents[3]

from kernel.agent_framework.config import (ConfigError,
    load_dotenv, load_yaml, require)
from kernel.console.config_admin import (
    build_config_schema, list_config_files, read_config_file,
    resolve_config_file, save_config_file, update_config_file)

logger = logging.getLogger("kernel.console")

# 匹配 kernel 的 logging 输出：'%(asctime)s %(levelname)-7s %(name)s: %(message)s'
_LOG_LINE_RE = re.compile(
    r"^(?P<time>[\d\- :,]+) (?P<level>\w+)\s+(?P<logger>[^\s:]+): "
    r"(?P<message>.*)$")

# console 自己处理的路径（其余 /api/* 走代理到 kernel）
_CONSOLE_PATHS = {
    "GET": {"/api/status", "/api/logs",
            "/api/config/files", "/api/config/file", "/api/config/schema"},
    "POST": {"/api/control/start", "/api/control/stop",
             "/api/control/restart", "/api/config/file",
             "/api/config/update"},
}

# 「配置」视图的读写/结构化编辑实现见 kernel.console.config_admin
# （resolve/list/read/save/build_config_schema/update_config_file，
# 由上方 import 引入，供本模块历史调用点与单测继续按原名使用）。

# ── 首启体验：.env 与鉴权 token 零配置自举 ─────────────────────────────
# .env 缺失时从 .env.example 生成骨架；RESIDENT_API_KEY /
# RESIDENT_CONSOLE_TOKEN 为缺失或占位（changeme）时自动生成强随机值，
# 写回 .env（新建文件权限 0600）并打印在启动日志里。

_ENV_TOKEN_KEYS = ("RESIDENT_API_KEY", "RESIDENT_CONSOLE_TOKEN")


def _env_line_value(lines: list[str], key: str) -> str:
    """从 .env 文本行里取某个键的当前值（注释行忽略，去引号）。"""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _set_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """把 .env 文本里某键的值替换为新值；键不存在则追加一行。"""
    updated: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            name = stripped.partition("=")[0].strip()
            if name == key:
                updated.append(f"{key}={value}")
                replaced = True
                continue
        updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    return updated


def _ensure_env_tokens(env_path: Path) -> dict[str, Any]:
    """保证 .env 存在且鉴权 token 可用（首次启动无需手填 token）。

    - .env 缺失：从同目录 .env.example 生成骨架（无 example 则新建）；
    - token 缺失或仍为 changeme：生成强随机值，写回 .env 并设置 os.environ；
    - LLM_* 仍为占位值时在返回值里标记，供启动日志提醒。

    返回 {"generated": {key: value}, "from_example": bool,
    "llm_placeholder": bool}。"""
    info: dict[str, Any] = {"generated": {}, "from_example": False,
                            "llm_placeholder": False}
    example = env_path.parent / ".env.example"
    existed = env_path.is_file()
    if existed:
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取 .env 失败：%s", exc)
            return info
    elif example.is_file():
        try:
            text = example.read_text(encoding="utf-8")
            info["from_example"] = True
        except OSError as exc:
            logger.warning("读取 .env.example 失败：%s", exc)
            return info
    else:
        text = ""
    lines = text.splitlines()
    for key in _ENV_TOKEN_KEYS:
        current = os.environ.get(key, "") or _env_line_value(lines, key)
        if current and current != "changeme":
            os.environ.setdefault(key, current)
            continue
        value = secrets.token_urlsafe(32)
        lines = _set_env_line(lines, key, value)
        os.environ[key] = value
        info["generated"][key] = value
    if info["generated"] or info["from_example"]:
        try:
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if not existed:
                os.chmod(env_path, 0o600)
        except OSError as exc:
            logger.warning("写入 .env 失败：%s", exc)
    placeholders = ("changeme", "your-")
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        value = os.environ.get(key, "") or _env_line_value(lines, key)
        if not value or any(mark in value for mark in placeholders):
            info["llm_placeholder"] = True
            break
    return info


class CoreManager:
    """Start / stop / restart the kernel subprocess; persist its logs."""

    def __init__(self, cfg: dict, log_file: Path, stop_timeout: float,
                 log_max_mb: float = 10.0,
                 log_archive_file: str = "") -> None:
        core = cfg["core"]
        self._executable = str(core["executable"])
        self._module = str(core["module"])
        self._args = [str(a) for a in core["args"]]
        self._log_file = Path(log_file)
        self._stop_timeout = float(stop_timeout)
        self._log_max_bytes = int(float(log_max_mb) * 1024 * 1024)
        self._log_archive = Path(log_archive_file
                                 or str(log_file) + ".archive")
        self._proc: Optional[subprocess.Popen] = None
        self._started_at: Optional[str] = None
        self._wanted = False  # kernel 是否应保持运行（人为停止后不再自动拉起）
        self._lock = threading.Lock()

    def _rotate_log_if_needed(self) -> None:
        try:
            if not self._log_file.is_file():
                return
            size = self._log_file.stat().st_size
            if size <= self._log_max_bytes:
                return
            with self._log_file.open("r", encoding="utf-8",
                                     errors="replace") as source:
                data = source.read()
            self._log_archive.parent.mkdir(parents=True, exist_ok=True)
            with self._log_archive.open("a", encoding="utf-8") as target:
                target.write(data)
            self._log_file.unlink(missing_ok=True)
            logger.info("kernel.log rotated to %s (%d bytes)",
                        self._log_archive, size)
        except OSError:
            logger.exception("kernel.log rotation failed")

    def status(self) -> dict:
        with self._lock:
            proc = self._proc
            started_at = self._started_at
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "pid": proc.pid if (running and proc is not None) else None,
            "started_at": started_at,
            "uptime": self._uptime(started_at) if running else None,
            "log_file": str(self._log_file),
        }

    @staticmethod
    def _uptime(started_at: Optional[str]) -> Optional[float]:
        if not started_at:
            return None
        try:
            start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            return round((datetime.now() - start).total_seconds(), 1)
        except ValueError:
            return None

    def start(self) -> dict:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": False, "error": "kernel 已在运行"}
            self._rotate_log_if_needed()
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            # 重新加载 .env（覆盖旧值）：让「编辑 .env → 重启 kernel」立即生效。
            # 否则子进程继承 console 启动时的旧环境变量，kernel 的 load_dotenv
            # 又不覆盖已存在的变量，改 .env 会“看起来像没生效”。
            load_dotenv(ROOT / ".env", override=True)
            # 子进程 stdio 固定 UTF-8：Windows 上被重定向的 stdio 默认走系统
            # 本地编码（GBK），中文日志会写坏（前端日志视图按 UTF-8 读取）。
            child_env = os.environ.copy()
            child_env["PYTHONUTF8"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
            log_handle = self._log_file.open("ab", buffering=0)
            cmd = [str(self._executable), "-m", self._module] + self._args
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=log_handle,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=child_env,
                )
            except OSError as exc:
                log_handle.close()
                return {"ok": False,
                        "error": f"启动 kernel 失败：{exc}"}
            self._proc = proc
            self._started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._wanted = True
            return {"ok": True, "pid": proc.pid}

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._started_at = None
            self._wanted = False
        if proc is None or proc.poll() is not None:
            return {"ok": False, "error": "kernel 未在运行"}
        try:
            proc.terminate()  # SIGTERM
            try:
                proc.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except OSError:
            pass
        return {"ok": True, "pid": proc.pid}

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def ensure_alive(self) -> dict:
        """Watchdog: when the kernel should be running but its process has
        exited unexpectedly, auto-restart it. Manual stop sets ``_wanted`` to
        False so it never resurrects after a deliberate shutdown."""
        with self._lock:
            wanted = self._wanted
            proc = self._proc
        alive = proc is not None and proc.poll() is None
        if wanted and not alive:
            logger.warning("kernel exited unexpectedly; auto-restarting")
            result = self.start()
            result["restarted"] = True
            return result
        return {"ok": True, "restarted": False}

    def tail(self, after: int = 0, limit: int = 500,
             level: str = "", query: str = "") -> dict:
        """Incremental tail of the persisted kernel log."""
        if not self._log_file.is_file():
            return {"offset": 0, "total": 0, "lines": []}
        try:
            size = self._log_file.stat().st_size
            start = max(0, min(after, size))
            with self._log_file.open("r", encoding="utf-8",
                                     errors="replace") as handle:
                handle.seek(start)
                chunk = handle.read(size - start)
        except OSError as exc:
            return {"offset": after, "total": 0, "error": str(exc)}
        offset = size
        if after > size:
            return {"offset": offset, "total": 0, "lines": []}
        level = str(level or "").strip().upper()
        query = str(query or "").strip().lower()
        lines: list[dict] = []
        for raw in chunk.splitlines():
            m = _LOG_LINE_RE.match(raw)
            if m:
                line = dict(m.groupdict())
            else:
                line = {"time": "", "thread": "", "level": "",
                        "logger": "", "message": raw}
            if level and line.get("level") != level:
                continue
            if query and query not in (line.get("message") or "").lower():
                continue
            lines.append(line)
        if limit and len(lines) > limit:
            lines = lines[-limit:]
        return {"offset": offset, "total": len(lines), "lines": lines}


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class ConsoleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    manager: CoreManager
    kernel_url: str = "http://127.0.0.1:18086"
    api_token: str = ""
    kernel_api_key: str = ""
    # 「配置」视图（相对路径在启动时已按仓库根解析为绝对路径）
    config_dir: str = ""
    config_backup_dir: str = ""
    config_edit_max_bytes: int = 0

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _authorized(self) -> bool:
        """Constant-time check of the console token from header or query.
        SSE (EventSource) cannot set headers, so it passes ``?token=``."""
        value = self.headers.get("X-Api-Token") or ""
        if not value:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                value = auth[7:].strip()
        if not value:
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            value = (qs.get("token") or [""])[0]
        return hmac.compare_digest(str(value or ""), str(self.api_token or ""))

    # ── helpers ─────────────────────────────────────────────────────────
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        dist = ROOT / "src" / "kernel" / "console" / "frontend" / "dist"
        index = dist / "index.html"
        if not index.is_file():
            body = (b"<h1>frontend not built</h1><p>cd frontend && "
                    b"npm install && npm run build</p>")
        else:
            body = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, path: str) -> None:
        dist = (ROOT / "src" / "kernel" / "console" / "frontend" / "dist")
        target = (dist / path.lstrip("/")).resolve()
        if not str(target).startswith(str(dist.resolve())) or not target.is_file():
            self._json(404, {"error": "asset not found"})
            return
        body = target.read_bytes()
        ctype = {".js": "application/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".woff2": "font/woff2", ".json": "application/json",
                 ".ico": "image/x-icon",
                 }.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ── reverse proxy to the kernel (single-port path routing) ─────────
    def _proxy_request(self, method: str, url: str,
                       body: Optional[bytes] = None,
                       content_type: Optional[str] = None) -> bool:
        """Forward to the kernel and relay the response. Returns True when
        the connection should stay OPEN (streaming SSE)."""
        req = urllib.request.Request(url, method=method,
                                     data=body if body is not None else None)
        if body is not None:
            # 透传原始 Content-Type（multipart/form-data 含 boundary；
            # 旧实现写死 application/json 会让 /api/image 上传失败）
            req.add_header("Content-Type",
                           content_type or "application/json")
        # kernel http_interface 需要自己的鉴权（注入 kernel_api_key）
        if self.kernel_api_key:
            req.add_header("X-Api-Token", self.kernel_api_key)
        try:
            stream: bool
            resp = urllib.request.urlopen(req, timeout=None)
            stream = "event-stream" in (
                resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            resp = exc
            stream = False
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": f"kernel 代理失败: {exc}"})
            return False
        self.send_response(resp.status)
        for header in ("Content-Type", "Cache-Control", "X-Accel-Buffering"):
            value = resp.headers.get(header)
            if value:
                self.send_header(header, value)
        self.send_header("X-Proxy-By", "resident-console")
        if not stream:
            chunk = resp.read()
            self.send_header("Content-Length", str(len(chunk)))
        else:
            # 流式：上游结束后必须显式关闭连接（无 Content-Length，
            # keep-alive 会让读取到 EOF 的客户端一直挂起）。浏览器
            # EventSource 遇到关闭会自动重连，属正常行为。
            self.send_header("Connection", "close")
        self.end_headers()
        if not stream:
            if chunk:
                self.wfile.write(chunk)
            resp.close()
            return False
        # SSE: relay line by line, flush each chunk so it streams through.
        # 浏览器断开（刷新/切页/EventSource 关闭）会让写客户端 socket 抛
        # BrokenPipe/ConnectionReset —— 这是常态，必须静默结束，别把异常
        # 抛回 socketserver 刷 traceback。
        while True:
            line = resp.readline()
            if not line:
                break
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break  # 客户端已断开，静默结束本路 SSE
        try:
            resp.close()
        except OSError:
            pass
        self.close_connection = True  # 结束流，客户端读到 EOF
        return True

    def _route(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = parsed.query
        method = self.command
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        if path.startswith("/assets/"):
            self._serve_asset(path)
            return
        # 所有 /api/* 必须鉴权（控制/日志/代理到 kernel 一律要求 token）
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if path in _CONSOLE_PATHS.get(method, set()):
            self._handle_console(path, query)
            return
        # 其余 /api/* 原样代理到 kernel（单端口，路径区分服务）
        url = self.kernel_url.rstrip("/") + path
        if query:
            url += "?" + query
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        self._proxy_request(method, url, body,
                            content_type=self.headers.get("Content-Type"))

    def _read_json_body(self) -> Optional[dict]:
        """读取并解析请求体的 JSON 对象；失败时已回 400，返回 None。"""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"ok": False, "error": "请求体需要 JSON"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "请求体需要 JSON 对象"})
            return None
        return payload

    def _handle_console(self, path: str, query: str) -> None:
        qs = urllib.parse.parse_qs(query)
        if path == "/api/status":
            payload = self.manager.status()
            payload["console"] = {"ok": True,
                                  "kernel_url": self.kernel_url}
            # 前端 ConsoleView 展示 “core 端口”
            payload["core_port"] = (
                urllib.parse.urlparse(self.kernel_url).port or 18086)
            self._json(200, payload)
            return
        if path == "/api/logs":
            try:
                after = int(qs.get("after", ["0"])[0])
            except ValueError:
                after = 0
            try:
                limit = int(qs.get("limit", ["500"])[0])
            except ValueError:
                limit = 500
            self._json(200, self.manager.tail(
                after=after, limit=limit,
                level=qs.get("level", [""])[0],
                query=qs.get("q", [""])[0],
            ))
            return
        if path == "/api/config/file" and self.command == "POST":
            payload = self._read_json_body()
            if payload is None:
                return
            code, result = save_config_file(
                self.config_dir, self.config_backup_dir,
                str(payload.get("path") or ""),
                payload.get("content"), self.config_edit_max_bytes)
            if code == 200:
                logger.info("config saved: %s (backup=%s)",
                            result.get("path"), result.get("backup"))
            self._json(code, result)
            return
        # 配置视图：YAML 结构化表单（schema 读取 / 按选项更新）
        if path == "/api/config/schema":
            rel = (qs.get("path") or [""])[0]
            code, payload = build_config_schema(
                self.config_dir, rel, self.config_edit_max_bytes)
            self._json(code, payload)
            return
        if path == "/api/config/update" and self.command == "POST":
            payload = self._read_json_body()
            if payload is None:
                return
            code, result = update_config_file(
                self.config_dir, self.config_backup_dir,
                str(payload.get("path") or ""), payload.get("updates"),
                self.config_edit_max_bytes)
            if code == 200:
                logger.info("config updated: %s (%d keys, backup=%s)",
                            result.get("path"),
                            len(result.get("updated") or []),
                            result.get("backup"))
            self._json(code, result)
            return
        # 配置视图：列表 / 读取（写走 POST）
        if path == "/api/config/files":
            self._json(200, {"ok": True,
                             "files": list_config_files(self.config_dir)})
            return
        if path == "/api/config/file":
            rel = (qs.get("path") or [""])[0]
            code, payload = read_config_file(
                self.config_dir, rel, self.config_edit_max_bytes)
            self._json(code, payload)
            return
        # control
        if path == "/api/control/start":
            self._json(200, self.manager.start())
            return
        if path == "/api/control/stop":
            self._json(200, self.manager.stop())
            return
        if path == "/api/control/restart":
            self._json(200, self.manager.restart())
            return
        self._json(404, {"error": f"unknown console path: {path}"})

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()


def load_console_config(path: str) -> dict:
    config = load_yaml(path)
    for key in ("console.host", "console.port", "console.log_file",
                "console.api_token",
                "console.kernel_api_key",
                "console.core.executable", "console.core.module",
                "console.core.args", "console.core.auto_start",
                "console.core.stop_timeout_seconds",
                "console.kernel_url",
                "console.config_dir", "console.config_backup_dir",
                "console.config_edit_max_bytes"):
        require(config, key)
    sandbox = config.get("sandbox") or {}
    if sandbox.get("enabled"):
        for key in ("sandbox.image", "sandbox.container",
                    "sandbox.workspace_host",
                    "sandbox.workspace_container", "sandbox.port"):
            require(config, key)
    return config


def _handle_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def ensure_sandbox(script: str, timeout: float = 180.0) -> dict:
    """Ensure Docker daemon + sandbox container are up via docker_up.sh.
    Non-fatal at boot: failures only log/skip (kernel can still run)."""
    if not Path(script).is_file():
        return {"ok": False, "error": f"script missing: {script}"}
    try:
        proc = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            timeout=float(timeout))
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0:
            logger.info("sandbox up: %s", output.splitlines()[-1]
                        if output else "")
        else:
            logger.warning("docker_up.sh exit=%d:\n%s",
                           proc.returncode, output)
        return {"ok": proc.returncode == 0, "output": output}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_sandbox failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Resident console")
    parser.add_argument("--config", default="config/console.yaml")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")  # 本地调试环境（.gitignore 已忽略）
    env_info = _ensure_env_tokens(ROOT / ".env")  # 首启自举：token 自动生成
    config = load_console_config(args.config)
    section = config["console"]
    if args.host:
        section["host"] = args.host
    if args.port:
        section["port"] = args.port
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: "
                               "%(message)s")
    manager = CoreManager(
        section, section["log_file"],
        stop_timeout=float(section["core"]["stop_timeout_seconds"]),
        log_max_mb=float(section.get("log_max_mb", 10.0)),
        log_archive_file=str(section.get("log_archive_file", "")))
    ConsoleHandler.manager = manager
    ConsoleHandler.kernel_url = str(section["kernel_url"])
    ConsoleHandler.api_token = str(section["api_token"])
    ConsoleHandler.kernel_api_key = str(section["kernel_api_key"])

    def _root_abs(relative: str) -> str:
        """相对路径按仓库根解析（console 可能从任意 CWD 启动）。"""
        candidate = Path(str(relative))
        return str(candidate if candidate.is_absolute()
                   else (ROOT / candidate).resolve())

    ConsoleHandler.config_dir = _root_abs(str(section["config_dir"]))
    ConsoleHandler.config_backup_dir = _root_abs(
        str(section["config_backup_dir"]))
    ConsoleHandler.config_edit_max_bytes = int(
        section["config_edit_max_bytes"])

    # 看门狗：kernel 非人为退出时自动重启，确保“存活”
    def _supervise() -> None:
        while True:
            time.sleep(2.0)
            try:
                manager.ensure_alive()
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor check failed: %s", exc)

    threading.Thread(target=_supervise, daemon=True).start()

    try:
        server = ConsoleHTTPServer(
            (str(section["host"]), int(section["port"])),
            ConsoleHandler)
    except OSError as exc:
        if getattr(exc, "errno", None) == 48:  # EADDRINUSE
            print(f"启动失败：端口 {section['port']} 已被占用"
                  "（可能已有 console 在运行）。先停止旧实例，或换端口。")
            return 1
        raise
    port = server.server_address[1]
    print(f"管理面板已启动，可访问：http://127.0.0.1:{port}/")
    if env_info["from_example"]:
        print("未找到 .env，已从 .env.example 生成一份")
    if env_info["generated"]:
        print("已自动生成鉴权密钥并写入 .env（请妥善保存）：")
        login_token = env_info["generated"].get("RESIDENT_CONSOLE_TOKEN")
        if login_token:
            print(f"  网页登录密钥（RESIDENT_CONSOLE_TOKEN）：{login_token}")
    if env_info["llm_placeholder"]:
        print("提示：.env 中 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 仍为"
              "占位值，请填写后重启（scripts/stop.sh && scripts/start.sh）。")
    signal.signal(signal.SIGTERM, _handle_sigterm)
    # 启动期确保 Docker/沙箱容器就绪（enabled=false 跳过；失败仅警告）
    if (config.get("sandbox") or {}).get("enabled"):
        ensure_sandbox(ROOT / "scripts" / "docker_up.sh",
                       timeout=float((config.get("sandbox") or {})
                                     .get("start_timeout_seconds", 180)))

    if bool(section.get("core", {}).get("auto_start")):
        result = manager.start()
        print(f"kernel auto-start: {'ok pid=%s' % result.get('pid')}"
              if result.get("ok")
              else f"kernel auto-start skipped: {result.get('error')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.stop()  # supervisor: 退出时关闭 kernel，避免孤儿进程

    return 0


if __name__ == "__main__":
    sys.exit(main())
