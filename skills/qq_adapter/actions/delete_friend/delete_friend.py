#!/usr/bin/env python3
"""delete_friend — 删好友（不可逆）

用法：
  python3 delete_friend.py <user_id> [--dry-run]

高风险动作，服务端默认禁用；要用先在 config/local.yaml 顶层 actions 段打开并重启。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="删好友（不可逆）")
    parser.add_argument("user_id", help="QQ 号")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"user_id": ns.user_id}

    call([element("delete_friend", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
