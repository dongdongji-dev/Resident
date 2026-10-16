#!/usr/bin/env python3
"""handle_group_request — 同意/拒绝入群申请

用法：
  python3 handle_group_request.py <flag> [--approve true] [--reason …] [--sub_type add] [--dry-run]

⚠️ 通知里的数字是截断值，会 ok:true 但没生效；先查真 flag。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="同意/拒绝入群申请")
    parser.add_argument("flag", help="申请的真 flag（从 group_system_msg 或消息 raw_segments 取）")
    parser.add_argument("--approve", default="true", help="true=同意，false=拒绝")
    parser.add_argument("--reason", default="", help="拒绝理由（可空）")
    parser.add_argument("--sub_type", default="add", help="add / invite（默认 add）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"flag": ns.flag, "approve": ns.approve, "reason": ns.reason, "sub_type": ns.sub_type}

    call([element("handle_group_request", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
