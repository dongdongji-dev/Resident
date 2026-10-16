#!/usr/bin/env python3
"""group_system_msg — 取群系统消息（入群申请等，含真 flag）

用法：
  python3 group_system_msg.py [--count 50] [--dry-run]

入群/加好友的真 flag 从这里取（通知里的那串数字是截断值，不能用）。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="取群系统消息（入群申请等，含真 flag）")
    parser.add_argument("--count", default="50", help="取多少条（默认 50）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"count": ns.count}

    call([element("get_group_system_msg", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
