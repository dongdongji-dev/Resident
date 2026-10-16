#!/usr/bin/env python3
"""doubt_friends_add_request — 列未处理的好友申请

用法：
  python3 doubt_friends_add_request.py [--count 50] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="列未处理的好友申请")
    parser.add_argument("--count", default="50", help="取多少条（默认 50）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"count": ns.count}

    call([element("get_doubt_friends_add_request", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
