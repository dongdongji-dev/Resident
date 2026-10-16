#!/usr/bin/env python3
"""set_special_title — 设专属头衔

用法：
  python3 set_special_title.py <conversation> <user_id> <title> [--clear] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="设专属头衔")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("user_id", help="谁的 QQ 号")
    parser.add_argument("title", nargs="?", default="", help="头衔文本（用 --clear 时可省略）")
    parser.add_argument("--clear", action="store_true", help="清空头衔（服务端要求 title 非空，这里用空格顶掉）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "user_id": ns.user_id, "title": ns.title}
    if ns.clear:
        attributes["title"] = " "
    call([element("set_special_title", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
