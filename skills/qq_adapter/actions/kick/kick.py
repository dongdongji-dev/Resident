#!/usr/bin/env python3
"""kick — 把成员移出群

用法：
  python3 kick.py <conversation> <user_id> [--reject_add] [--dry-run]

需要管理员/群主。napcat 有时返回成功但实际没生效，以群里的实际情况为准。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="把成员移出群")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("user_id", help="被踢的 QQ 号")
    parser.add_argument("--reject_add", action="store_true", help="同时拒绝此人再次申请")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "user_id": ns.user_id}
    if ns.reject_add:
        attributes["reject_add"] = "true"
    call([element("kick", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
