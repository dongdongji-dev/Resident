#!/usr/bin/env python3
"""set_msg_emoji_like — 给某条消息贴表情

用法：
  python3 set_msg_emoji_like.py <message_id> <emoji_id> [--set true] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="给某条消息贴表情")
    parser.add_argument("message_id", help="消息 id")
    parser.add_argument("emoji_id", help="表情 id")
    parser.add_argument("--set", default="true", help="true=贴上，false=取消")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"message_id": ns.message_id, "emoji_id": ns.emoji_id, "set": ns.set}

    call([element("set_msg_emoji_like", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
