#!/usr/bin/env python3
"""set_card — 改群名片

用法：
  python3 set_card.py <conversation> <user_id> <card> [--dry-run]

改自己名片也要走这里；改别人需要管理员。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="改群名片")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("user_id", help="谁的 QQ 号")
    parser.add_argument("card", help="新名片")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "user_id": ns.user_id, "card": ns.card}

    call([element("set_card", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
