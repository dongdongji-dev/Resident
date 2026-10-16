#!/usr/bin/env python3
"""delete_group_notice — 删群公告

用法：
  python3 delete_group_notice.py <conversation> <notice_id> [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="删群公告")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("notice_id", help="公告 id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "notice_id": ns.notice_id}

    call([element("delete_group_notice", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
