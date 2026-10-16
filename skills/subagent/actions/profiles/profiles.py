#!/usr/bin/env python3
"""profiles.py — 列出可用模型 profile。GET /api/profiles

用法：python3 profiles/profiles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    sys.exit(_hook.emit(_hook.request("GET", "/api/profiles", timeout=15)))
