#!/usr/bin/env python3
"""ask_reply.py — 一次性答复实例的 ask 提问（或 cancel 取消本轮）。POST /api/agents/<name>/ask_reply

用法：python3 ask_reply/ask_reply.py --name <实例名> --ask-id <ID> --answers '{"1":"A","2":"B"}'  |  --cancel
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

import json  # noqa: E402

if __name__ == "__main__":
    kw = _hook.parse_kv(sys.argv[1:])
    name = kw.get("name") or kw.get("agent")
    aid = kw.get("ask-id") or kw.get("ask_id")
    if not name or not aid:
        _hook.die("缺 --name 或 --ask-id")
    if kw.get("cancel"):
        sys.exit(_hook.emit(_hook.request(
            "POST", f"/api/agents/{name}/ask_reply",
            {"ask_id": aid, "cancel": True}, timeout=30)))
    answers = kw.get("answers")
    if not answers:
        _hook.die("缺 --answers（JSON，如 '{\"1\":\"A\"}'）或 --cancel")
    try:
        parsed = json.loads(answers)
    except ValueError as exc:
        _hook.die(f"--answers 不是合法 JSON: {exc}")
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/ask_reply",
        {"ask_id": aid, "answers": parsed}, timeout=30)))
