#!/usr/bin/env python3
"""agents.py — 列出已注册实例（status 的 agents 子集，好读）。GET /api/status

用法：python3 agents/agents.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    res = _hook.request("GET", "/api/status", timeout=15)
    if res.get("ok"):
        res = {"ok": True, "agents": res.get("agents", [])}
    sys.exit(_hook.emit(res))
