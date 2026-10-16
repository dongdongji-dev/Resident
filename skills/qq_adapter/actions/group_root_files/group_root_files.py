#!/usr/bin/env python3
"""group_root_files — 列群文件（根目录）

用法：
  python3 group_root_files.py <conversation> [--file_count 50] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="列群文件（根目录）")
    parser.add_argument("conversation", help="群会话键 group_xxx")
    parser.add_argument("--file_count", default="50", help="取多少个（默认 50）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "file_count": ns.file_count}

    call([element("get_group_root_files", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
