#!/usr/bin/env python3
"""restore.py — 把实例回滚到某个 checkpoint。POST /api/agents/<name>/restore

用法：python3 restore/restore.py --name <实例名> [--scope all|conv|work] [--ref <commit>]
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
    body = {"scope": kw.get("scope", "all")}
    if kw.get("ref"):
        body["ref"] = kw["ref"]
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/restore", body, timeout=60)))
