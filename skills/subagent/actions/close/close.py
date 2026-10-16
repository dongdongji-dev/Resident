#!/usr/bin/env python3
"""close.py — 关闭一个实例（释放内存；context 留在磁盘）。POST /api/agents/<name>/close

用法：python3 close/close.py --name <实例名>
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
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/close", {}, timeout=30)))
