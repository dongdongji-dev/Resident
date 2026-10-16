#!/usr/bin/env python3
"""attention_get — 查当前注意力群。

用法：
  python3 attention_get.py

注意力群 = 该群所有新消息都唤醒我（不用 @）。同时只可能有一个；
返回 {conversation_id, last_active, expires_in_seconds}，空串＝没设。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element  # noqa: E402


def main() -> int:
    call([element("get_attention")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
