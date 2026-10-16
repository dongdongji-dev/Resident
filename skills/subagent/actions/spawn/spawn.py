#!/usr/bin/env python3
"""spawn.py — 注册（spawn）一个新 subagent 实例。POST /api/agents

用法：python3 spawn/spawn.py --name <实例名> [--system-prompt 文本|--system-prompt-file 路径] [--profile default] [--workdir /路径]
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
    body = {"name": name}
    if kw.get("system-prompt"):
        body["system_prompt"] = kw["system-prompt"]
    elif kw.get("system-prompt-file"):
        body["system_prompt"] = Path(kw["system-prompt-file"]).read_text(
            encoding="utf-8")
    if kw.get("profile") or kw.get("model-profile"):
        body["model_profile"] = kw.get("profile") or kw["model-profile"]
    if kw.get("workdir"):
        body["workdir"] = kw["workdir"]
    sys.exit(_hook.emit(_hook.request("POST", "/api/agents", body, timeout=60)))
