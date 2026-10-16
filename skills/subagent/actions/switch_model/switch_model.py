#!/usr/bin/env python3
"""switch_model.py — 给实例换模型 profile（可顺带换 system_prompt）。POST /api/agents/<name>/switch_model

用法：python3 switch_model/switch_model.py --name <实例名> --profile <profile名> [--system-prompt 文本]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

if __name__ == "__main__":
    kw = _hook.parse_kv(sys.argv[1:])
    name = kw.get("name") or kw.get("agent")
    profile = kw.get("profile") or kw.get("model-profile")
    if not name or not profile:
        _hook.die("缺 --name 或 --profile")
    body = {"model_profile": profile}
    if kw.get("system-prompt"):
        body["system_prompt"] = kw["system-prompt"]
    sys.exit(_hook.emit(_hook.request(
        "POST", f"/api/agents/{name}/switch_model", body, timeout=30)))
