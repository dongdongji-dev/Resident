#!/usr/bin/env python3
"""emoji_likes — 查某条消息的贴表情情况

用法：
  python3 emoji_likes.py <message_id> <emoji_id> [--count 0] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="查某条消息的贴表情情况")
    parser.add_argument("message_id", help="消息 id")
    parser.add_argument("emoji_id", help="表情 id")
    parser.add_argument("--count", default="0", help="取多少个（默认 0=不限）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"message_id": ns.message_id, "emoji_id": ns.emoji_id, "count": ns.count}

    call([element("get_emoji_likes", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
