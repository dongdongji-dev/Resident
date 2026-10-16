#!/usr/bin/env python3
"""_hook.py — actions/ 共用小工具：找服务端口、读 token、发一次 /hook、打印结果。

别的动作脚本只 import 这个，别各自重复实现。
定位服务的顺序：环境变量 QQ_ADAPTER_STATE_DIR → /home/sandbox/skill_data/qq_adapter，
读里面的 port 文件；没有就退回环境变量 QQ_ADAPTER_PORT / 19671。
token：HOOK_TOKEN 环境变量 → .secrets/hook_token.txt → .secrets/core_api_key.txt。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SANDBOX = Path("/home/sandbox")


def state_dir() -> Path:
    return Path(os.environ.get("QQ_ADAPTER_STATE_DIR",
                               SANDBOX / "skill_data" / "qq_adapter"))


def hook_url() -> str:
    if os.environ.get("HOOK_URL"):
        return os.environ["HOOK_URL"]
    port_file = state_dir() / "port"
    port = ""
    if port_file.is_file():
        port = port_file.read_text(encoding="utf-8").strip()
    port = port or os.environ.get("QQ_ADAPTER_PORT", "19671")
    return f"http://127.0.0.1:{port}/hook"


def token() -> str:
    if os.environ.get("HOOK_TOKEN"):
        return os.environ["HOOK_TOKEN"]
    for name in ("hook_token.txt", "core_api_key.txt"):
        path = SANDBOX / ".secrets" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if "=" in line:
                line = line.split("=", 1)[1].strip()
            line = line.strip('"\'')
            if line:
                return line
    raise RuntimeError("缺 token：设 HOOK_TOKEN，或写 "
                       "/home/sandbox/.secrets/hook_token.txt")


def element(tag: str, attributes: dict | None = None,
            content: list | None = None) -> dict:
    """拼一个动作元素；content 传的是子元素列表（如 send_messages 的段）。"""
    item: dict = {"tag": tag, "attributes": {k: str(v) for k, v in
                                             (attributes or {}).items()}}
    if content is not None:
        item["content"] = content
    return item


def call(elements: list, dry_run: bool = False, timeout: float = 60.0) -> dict:
    """把动作元素发给常驻服务，打印结果 JSON 并返回。"""
    payload = {"action": {"elements": elements}}
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    request = urllib.request.Request(
        hook_url(), data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Token": token()},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"✗ 连不上适配器 {hook_url()}：{exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def text_segment(text: str) -> dict:
    """QQ 文本框段（send_messages 的 content 用）。"""
    return {"tag": "text", "attributes": {}, "content": [{"text": text}]}


def segment(tag: str, attributes: dict | None = None) -> dict:
    """send_messages 里的普通段（at / reply / image / file / poke / face …）。"""
    return {"tag": tag,
            "attributes": {k: str(v) for k, v in (attributes or {}).items()}}
