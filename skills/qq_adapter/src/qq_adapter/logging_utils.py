"""Standalone logging for the napcat adapter (NO agent_runtime dependency).

The adapter is designed to run OUTSIDE the runtime project (own folder,
own venv): only the standard library is imported here.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import urllib.request
from typing import Any, TextIO


def configure_logging(level: str, log_format: str,
                      stream: TextIO = sys.stderr) -> None:
    """Configure the process root logger from explicit values."""
    numeric_level = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"unknown log level: {level}")
    if log_format == "text":
        formatter = logging.Formatter(
            "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s"
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s"
        )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger carrying stable structured context fields."""
    return logging.LoggerAdapter(logging.getLogger(name), {"context": context})


class HTTPLogHandler(logging.Handler):
    """异步批量上报日志到 core（远程 adapter 汇总查看）。

    攒批策略：队列满（batch_size）或距上次上报超 interval 秒即 flush。
    """

    def __init__(self, core_url: str, api_key: str, name: str,
                 batch_size: int = 10, interval_seconds: float = 2.0) -> None:
        super().__init__()
        self._core_url = str(core_url).rstrip("/")
        self._api_key = str(api_key)
        self._name = str(name)
        self._batch_size = max(1, int(batch_size))
        self._interval = float(interval_seconds)
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="napcat-http-log")
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait({
                "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except queue.Full:
            pass

    def _run(self) -> None:
        pending: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
                pending.append(item)
            except queue.Empty:
                pass
            due = len(pending) >= self._batch_size                 or (pending and time.time() - last_flush >= self._interval)
            if due and pending:
                self._flush(pending)
                pending = []
                last_flush = time.time()
        if pending:
            self._flush(pending)

    def _flush(self, entries: list[dict]) -> None:
        body = json.dumps({"name": self._name, "entries": entries},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._core_url}/api/v1/logs", data=body,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Api-Token": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except Exception:  # noqa: BLE001
            pass  # 日志上报尽力而为，失败静默（本地日志仍在）

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)


def enable_http_logging(core_url: str, api_key: str, name: str) -> None:
    """根 logger 挂 HTTP 上报 handler（远程 adapter 日志汇总）。"""
    root = logging.getLogger()
    handler = HTTPLogHandler(core_url, api_key, name)
    root.addHandler(handler)
