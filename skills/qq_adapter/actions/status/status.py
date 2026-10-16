#!/usr/bin/env python3
"""status — 看适配器状态。

用法：
  python3 status.py

返回 {online, good, stat}：online=连上 napcat，good=服务整体正常。
排查顺序就这一条：先跑这个，再 go_status / 看日志 state_dir/adapter.log。
另外裸 HTTP 也有一份：curl http://127.0.0.1:19671/status
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element  # noqa: E402


def main() -> int:
    call([element("get_status")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
