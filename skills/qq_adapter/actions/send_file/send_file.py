#!/usr/bin/env python3
"""send_file — 往一个会话发一个文件。

用法：
  python3 send_file.py group_200000001 /home/sandbox/download/a.zip
  python3 send_file.py private_1000000002 /home/sandbox/x.pdf --name 报告.pdf
  python3 send_file.py group_200000001 /home/sandbox/a.zip --dry-run

⚠️ 文件路径必须是**绝对路径**（相对路径会被静默丢弃，只报"没有可发送的段"）。
大文件自动走 adapter 的文件服务转 URL，小文件 base64 直发，无需关心。

📥 发完文件后数秒内，入站会出现一条**空内容事件**（无文本、无 segments，
adapter 记成"[系统通知] 空内容消息事件"）——这是**对方下载了你发的文件**的回执信号，
不是新消息、不需要回复；连续出现＝多次下载。发图片/文字不会触发它。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element, segment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发文件")
    parser.add_argument("conversation", help="会话键：group_xxx / private_xxx")
    parser.add_argument("path", help="文件绝对路径")
    parser.add_argument("--name", help="对方看到的文件名（默认用原名）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.path.startswith("/"):
        print("✗ 必须是绝对路径（如 /home/sandbox/...）", file=sys.stderr)
        return 2
    attributes = {"file": args.path}
    if args.name:
        attributes["name"] = args.name

    call([element("send_messages", {"conversation_id": args.conversation},
                  [segment("file", attributes)])],
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
