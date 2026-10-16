#!/usr/bin/env python3
"""set_essence — 把某条消息设为群精华

用法：
  python3 set_essence.py <conversation> <message_id> [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="把某条消息设为群精华")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("message_id", help="消息 id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "message_id": ns.message_id}

    call([element("set_essence", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
