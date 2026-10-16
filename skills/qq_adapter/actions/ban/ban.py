#!/usr/bin/env python3
"""ban — 群禁言（或解除）

用法：
  python3 ban.py <conversation> <user_id> [--duration 0] [--whole] [--enable] [--dry-run]

需要管理员/群主。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="群禁言（或解除）")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("user_id", help="被禁言的 QQ 号")
    parser.add_argument("--duration", default="0", help="禁言秒数，0=解除禁言")
    parser.add_argument("--whole", action="store_true", help="切换全员禁言（忽略 user_id/duration）")
    parser.add_argument("--enable", action="store_true", help="配合 --whole：开启全员禁言")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "user_id": ns.user_id, "duration": ns.duration}
    if ns.whole:
        attributes["whole"] = "true"
    if ns.enable:
        attributes["enable"] = "true"
    call([element("ban", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
