"""kernel.console — control plane (supervisor + single-port frontend).

A separate process that:
- one-click starts / stops / restarts the kernel and keeps it alive;
- persists the kernel's stdout/stderr into data/logs/kernel.log (rotating);
- serves the SPA frontend (Console / Chat / Log views) and, on ONE port,
  routes every service by path: own control+log endpoints and a streaming
  reverse proxy to the kernel's http_interface.
"""

from __future__ import annotations

__all__ = ["main"]
