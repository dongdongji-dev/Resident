#!/usr/bin/env python3
"""status.py — 看 hub 是否在跑 + 所有实例状态。GET /api/status

用法：python3 status/status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    sys.exit(_hook.emit(_hook.request("GET", "/api/status", timeout=15)))
