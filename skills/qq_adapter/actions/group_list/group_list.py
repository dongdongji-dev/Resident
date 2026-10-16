#!/usr/bin/env python3
"""group_list — 列出我加入的所有群

用法：
  python3 group_list.py [--dry-run]

返回群号/群名/人数。

属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="列出我加入的所有群")

    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

    attributes: dict = {}

    call([element("get_group_list", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
