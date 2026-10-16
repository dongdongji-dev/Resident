#!/usr/bin/env python3
"""recall — 撤回一条消息。

用法：
  python3 recall.py 1740909321
  python3 recall.py 1740909321 --dry-run

能撤谁的消息：自己的随时能撤；管理员/群主还能撤权限比你低的成员的消息
（权限不够时 napcat 会返回失败，别假定一定撤得掉）。
消息 id 从哪来：send_text/send_file 等返回的 message_id，
或 messages_query 查消息库的 remote_message_id。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="撤回消息")
    parser.add_argument("message_id", help="要撤回的消息 id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    call([element("delete_msg", {"message_id": args.message_id})],
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
