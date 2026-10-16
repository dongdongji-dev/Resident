"""Logging bootstrap (level from config).

Level policy:
- INFO = low-frequency, meaningful events: event queued, model output,
  model reasoning, tool usage, round lifecycle.
- DEBUG = high-frequency noise: per-request HTTP access (aiohttp.access),
  queue polls, tool params dumps.
- library loggers (aiohttp.*) are demoted so their INFO access lines do NOT
  flood the INFO stream (still visible when the app runs at DEBUG).
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

# 高频库 logger：其 INFO 访问日志在 INFO 模式下被降到 DEBUG
_NOISY_LOGGERS = ("aiohttp.access", "aiohttp.server",
                  "aiohttp.client", "aiohttp.web")


class _DowngradeHighFreqFilter(logging.Filter):
    """Lower INFO/lesser records from noisy loggers down to DEBUG."""

    def __init__(self, names: tuple[str, ...],
                 to_level: int = logging.DEBUG) -> None:
        super().__init__()
        self._names = set(names)
        self._to_level = to_level

    def filter(self, record: logging.LogRecord) -> bool:
        if (record.name in self._names
                and record.levelno < logging.WARNING):  # DEBUG/INFO → DEBUG
            record.levelno = self._to_level
            record.levelname = logging.getLevelName(self._to_level)
        return True


_DEMOTED = set()


def _demote_noisy_loggers() -> None:
    """Attach a demoting filter to each noisy logger exactly once.

    The filter runs on the LOGGER (before the handler's level gate), so an
    aiohttp INFO access line becomes DEBUG and then is dropped by the INFO
    handler; at DEBUG runtime it still shows. Keeps WARNING/ERROR intact.
    """
    global _DEMOTED
    for name in _NOISY_LOGGERS:
        if name in _DEMOTED:
            continue
        lg = logging.getLogger(name)
        lg.addFilter(_DowngradeHighFreqFilter(_NOISY_LOGGERS))
        lg.setLevel(logging.DEBUG)
        _DEMOTED.add(name)


def configure_logging(level: str = "INFO", format: str = "text") -> None:
    global _CONFIGURED
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(level.upper())  # handler 门：INFO 运行期丢 DEBUG
    root = logging.getLogger()
    if _CONFIGURED:
        root.handlers[:] = [handler]
    else:
        root.addHandler(handler)
        root.setLevel(level.upper())
        _CONFIGURED = True
    _demote_noisy_loggers()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
