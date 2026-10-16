#!/usr/bin/env python3
"""poke — 戳一戳某人。群聊走 group_poke，私聊走 friend_poke。

用法：
  python3 poke.py group_200000001 1000000002            # 在群里戳某人
  python3 poke.py private_1000000002 1000000002          # 私聊戳
  python3 poke.py group_200000001 1000000002 --dry-run

conversation 决定走群戳还是私聊戳，target_id 是被戳的人。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="戳一戳")
    parser.add_argument("conversation", help="会话键：group_xxx / private_xxx")
    parser.add_argument("target_id", help="被戳的 QQ 号")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    call([element("poke", {"conversation_id": args.conversation,
                           "target_id": args.target_id})],
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
