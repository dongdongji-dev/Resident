#!/usr/bin/env python3
"""_hook.py — actions/ 共用小工具：找 hub 端口、读 token、发一次 HTTP、打印结果。

别的动作脚本只 import 这个，别各自重复实现。
定位 hub 的顺序：
  环境变量 SUBAGENT_HUB_URL → SUBAGENT_HUB_PORT / hub.port 配置
  → state_dir（SUBAGENT_STATE_DIR，默认 /home/sandbox/skill_data/subagent）/port 文件
  → 默认 127.0.0.1:19711
token（hub 开了 api_key 才需要）：
  SUBAGENT_HUB_TOKEN 环境变量 → .secrets/subagent.env 里的 SUBAGENT_HUB_TOKEN
  → .secrets/subagent_hub_token.txt（纯值或 key=value）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SANDBOX = Path("/home/sandbox")
SKILL_DIR = Path(__file__).resolve().parent.parent


def base_url() -> str:
    if os.environ.get("SUBAGENT_HUB_URL"):
        return os.environ["SUBAGENT_HUB_URL"].rstrip("/")
    port = os.environ.get("SUBAGENT_HUB_PORT", "")
    if not port:
        state = Path(os.environ.get("SUBAGENT_STATE_DIR",
                                    SANDBOX / "skill_data" / "subagent"))
        port_file = state / "port"
        if port_file.is_file():
            port = port_file.read_text(encoding="utf-8").strip()
    return f"http://127.0.0.1:{port or '19711'}"


def token() -> str:
    if os.environ.get("SUBAGENT_HUB_TOKEN") is not None:
        return os.environ["SUBAGENT_HUB_TOKEN"]
    env_file = SANDBOX / ".secrets" / "subagent.env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            line = line.strip()
            if line.startswith("SUBAGENT_HUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"\'')
    plain = SANDBOX / ".secrets" / "subagent_hub_token.txt"
    if plain.is_file():
        text = plain.read_text(encoding="utf-8", errors="replace").strip()
        return text.split("=", 1)[1].strip() if "=" in text else text
    return ""


def request(method: str, path: str, body: dict | None = None,
            timeout: float = 600) -> dict:
    """发一次 HTTP；返回解析后的 dict（HTTP 错误也返回 dict，附 http_status）。"""
    url = base_url() + path
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    headers = {"Content-Type": "application/json"}
    tok = token()
    if tok:
        headers["X-Api-Token"] = tok
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            out = json.loads(raw) if raw.strip() else {}
            out.setdefault("http_status", resp.status)
            return out
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            out = json.loads(raw)
        except ValueError:
            out = {"ok": False, "error": raw[:500]}
        out["http_status"] = exc.code
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "http_status": 0,
                "hint": "hub 没起？先跑 bash start.sh status / start"}


def emit(result: dict, code: int | None = None) -> int:
    """统一输出 + 退出码（ok=false → 非 0）。"""
    ok = bool(result.get("ok"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if code is not None:
        return code
    return 0 if ok else 1


def die(msg: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(code)


def parse_kv(args: list[str]) -> dict:
    """把 --key=value / --key value / --flag 解析成 dict（供各动作脚本用）。

    ⚠️ 纯开关（后面跟另一个 --xxx 或到末尾）解析为 True，不是空串
    （否则 `--dry-run` / `--reject` 这类开关会被当假值）。
    """
    out: dict = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1)
            else:
                k = a[2:]
                nxt = args[i + 1] if i + 1 < len(args) else None
                if nxt is None or nxt.startswith("--"):
                    v = True
                else:
                    v = nxt
                    i += 1
            out[k] = v
        i += 1
    return out
