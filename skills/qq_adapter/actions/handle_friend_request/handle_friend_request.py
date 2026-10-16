#!/usr/bin/env python3
"""handle_friend_request — 同意/拒绝好友申请

用法：
  python3 handle_friend_request.py <flag> [--approve true] [--remark …] [--dry-run]

⚠️ 通知里带的那串数字是截断值，用了会 ok:true 但没生效——flag 必须先查。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="同意/拒绝好友申请")
    parser.add_argument("flag", help="申请的真 flag（从 doubt_friends_add_request 取）")
    parser.add_argument("--approve", default="true", help="true=同意，false=拒绝")
    parser.add_argument("--remark", default="", help="加好友后的备注（可空）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"flag": ns.flag, "approve": ns.approve, "remark": ns.remark}

    call([element("handle_friend_request", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
