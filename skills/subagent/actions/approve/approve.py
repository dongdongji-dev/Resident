#!/usr/bin/env python3
"""approve.py — 审批实例的越界 exec/文件操作请求。POST /api/agents/<name>/approve

用法：python3 approve/approve.py --name <实例名> --request-id <ID> [--reject]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    kw = _hook.parse_kv(sys.argv[1:])
    name = kw.get("name") or kw.get("agent")
    rid = kw.get("request-id") or kw.get("request_id")
    if not name or not rid:
        _hook.die("缺 --name 或 --request-id")
    approved = not (kw.get("reject") or kw.get("approved") in ("false", "0"))
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/approve",
        {"request_id": rid, "approved": approved}, timeout=30)))
