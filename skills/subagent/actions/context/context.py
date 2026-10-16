#!/usr/bin/env python3
"""context.py — 读实例的 context.jsonl（最后 N 条）。GET /api/agents/<name>/context?limit=N

用法：python3 context/context.py --name <实例名> [--limit 50]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    kw = _hook.parse_kv(sys.argv[1:])
    name = kw.get("name") or kw.get("agent")
    if not name:
        _hook.die("缺 --name（实例名）")
    limit = kw.get("limit", "50")
    sys.exit(_hook.emit(_hook.request(
        "GET", f"/api/agents/{name}/context?limit={limit}", timeout=30)))
