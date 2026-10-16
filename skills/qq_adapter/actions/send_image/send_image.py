#!/usr/bin/env python3
"""send_image — 往一个会话发一张图片。

用法：
  python3 send_image.py group_200000001 /home/sandbox/shot.png
  python3 send_image.py private_1000000002 /home/sandbox/a.jpg --dry-run

⚠️ 绝对路径。gif/表情包也走这里。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element, segment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发图片")
    parser.add_argument("conversation", help="会话键：group_xxx / private_xxx")
    parser.add_argument("path", help="图片绝对路径")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.path.startswith("/"):
        print("✗ 必须是绝对路径（如 /home/sandbox/...）", file=sys.stderr)
        return 2

    call([element("send_messages", {"conversation_id": args.conversation},
                  [segment("image", {"file": args.path})])],
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
