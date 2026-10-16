#!/usr/bin/env python3
"""send_group_notice — 发群公告

用法：
  python3 send_group_notice.py <conversation> <content> [--pinned 0] [--type 1] [--dry-run]

需要管理员。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="发群公告")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("content", help="公告正文")
    parser.add_argument("--pinned", default="0", help="1=置顶，0=不置顶")
    parser.add_argument("--type", default="1", help="公告类型（默认 1）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "content": ns.content, "pinned": ns.pinned, "type": ns.type}

    call([element("send_group_notice", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
