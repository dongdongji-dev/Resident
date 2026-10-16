#!/usr/bin/env python3
"""prompt.py — 给实例派一条任务（异步：它跑完一轮会经 notice 回报）。POST /api/agents/<name>/prompt

用法：python3 prompt/prompt.py --name <实例名> --content 文本（或用 --file 路径）[--wait 秒]
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
    content = kw.get("content")
    if not content and kw.get("file"):
        content = Path(kw["file"]).read_text(encoding="utf-8")
    if not content:
        _hook.die("缺 --content（或 --file 路径）")
    wait = float(kw.get("wait", 30) or 30)
    res = _hook.request("POST", f"/api/agents/{name}/prompt",
                        {"content": content}, timeout=wait)
    sys.exit(_hook.emit(res))
