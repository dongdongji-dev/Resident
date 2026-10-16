#!/usr/bin/env python3
"""send_forward — 把某条消息转发到会话

用法：
  python3 send_forward.py <conversation> <message_id> [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="把某条消息转发到会话")
    parser.add_argument("conversation", help="目标会话键")
    parser.add_argument("message_id", help="被转发的消息 id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "message_id": ns.message_id}

    call([element("send_forward", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
