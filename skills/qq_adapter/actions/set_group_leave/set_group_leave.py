#!/usr/bin/env python3
"""set_group_leave — 退群（或解散群）

用法：
  python3 set_group_leave.py <conversation> [--dismiss] [--dry-run]

高风险动作，服务端默认禁用；退群前先跟约定确认。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="退群（或解散群）")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("--dismiss", action="store_true", help="解散该群（需群主，不可逆）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation}
    if ns.dismiss:
        attributes["dismiss"] = "true"
    call([element("set_group_leave", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
