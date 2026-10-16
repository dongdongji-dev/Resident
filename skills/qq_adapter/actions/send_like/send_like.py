#!/usr/bin/env python3
"""send_like — 给某人资料卡点赞

用法：
  python3 send_like.py <user_id> [--times 1] [--dry-run]

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="给某人资料卡点赞")
    parser.add_argument("user_id", help="QQ 号")
    parser.add_argument("--times", default="1", help="点几次（默认 1）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes = {"user_id": ns.user_id, "times": ns.times}

    call([element("send_like", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
