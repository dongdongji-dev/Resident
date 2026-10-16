"""kernel.http_interface — the Kernel's HTTP Interface subsystem.

One HTTP server on ONE port (from config, never random); every service is a
separate path. This layer consumes the Agent Framework (agent + StreamBus)
and exposes it over HTTP/SSE; the framework itself knows nothing about HTTP.
"""

from __future__ import annotations

from kernel.http_interface.eventbus import StreamBus
from kernel.http_interface.server import build_app, start_server

__all__ = ["StreamBus", "build_app", "start_server"]
