#!/usr/bin/env python3
"""compress.py — 手动压缩实例上下文（按 compression 配置，可能耗时）。POST /api/agents/<name>/compress

用法：python3 compress/compress.py --name <实例名> [--wait 600]
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
    wait = float(kw.get("wait", 600) or 600)
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/compress", {}, timeout=wait)))
