#!/usr/bin/env python3
"""group_member_info — 查某人在群里的信息（昵称/名片/身份/入群时间）

用法：
  python3 group_member_info.py <conversation> <user_id> [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="查某人在群里的信息（昵称/名片/身份/入群时间）")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("user_id", help="被查的 QQ 号")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "user_id": ns.user_id}

    call([element("get_group_member_info", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
