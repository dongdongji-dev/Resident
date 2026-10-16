#!/usr/bin/env python3
"""file_url — 取群/私聊文件下载直链

用法：
  python3 file_url.py <conversation> <file_id> [--dry-run]

直链带时效（rkey），放久了取不到。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="取群/私聊文件下载直链")
    parser.add_argument("conversation", help="会话键（决定走群还是私聊）")
    parser.add_argument("file_id", help="文件 id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"conversation_id": ns.conversation, "file_id": ns.file_id}

    call([element("get_file_url", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
