#!/usr/bin/env python3
r"""send_msg.py 入口防呆的参数校验回归测试。

背景：2026-10-04 10:0x 实际踩坑——把 `send_msg.py --to <qq> --file <f>` 当成正确用法，
脚本把 `--to` 当会话名、qq 号当正文发了出去（adapter 返回 ok:false 才没真发成）。
本次给 send_msg.py 加了三条入口校验：① 第 1 参数必须以会话键开头（非选项）；
② 会话键格式 `^[A-Za-z][A-Za-z0-9_]*_[^_\s-]+$`；③ 未知 `--xxx` 选项直接中止。

本测试全部走 `--dry-run`（不真发消息），只校验参数解析层的返回码/stderr。
运行：python3 .skills/qq_adapter/tests/test_send_msg_args.py
"""
from __future__ import annotations

import subprocess
import sys

SEND = "/home/sandbox/scripts/send_msg.py"


def run(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run([sys.executable, SEND, *args], input=stdin,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


# (名称, argv, stdin, 期望返回码, stderr 应含的子串或 None)
CASES = [
    ("选项当会话名(--to)", ["--to", "1000000002"], None, 2, "应是会话键"),
    ("纯数字当会话名", ["12345", "hi"], None, 2, "会话键格式可疑"),
    ("未知选项--file", ["private_1000000002", "hi", "--file", "x.txt"], None, 2, "未知选项"),
    ("正文位置写成选项", ["private_1000000002", "--reply", "123", "-"], None, 2, "参数顺序错"),
    ("正常私聊 dry-run", ["private_1000000002", "hi", "--dry-run"], None, 0, None),
    ("正常群聊+at dry-run", ["group_200000001", "hi", "--at", "1000000002", "--dry-run"], None, 0, None),
    ("正常回复 dry-run", ["private_1000000002", "hi", "--reply", "123", "--dry-run"], None, 0, None),
    ("正常 stdin dry-run", ["private_1000000002", "-", "--dry-run"], "多行\n正文", 0, None),
    ("bili 会话键 dry-run", ["bili_private_7176006", "hi", "--dry-run"], None, 0, None),
    ("参数不足", ["private_1000000002"], None, 1, None),
]


def main() -> int:
    bad = 0
    for name, argv, stdin, want_rc, want_sub in CASES:
        rc, out, err = run(argv, stdin)
        ok = rc == want_rc and (want_sub is None or want_sub in err)
        if not ok:
            bad += 1
            print(f"✗ {name}: rc={rc}（期望 {want_rc}）stderr={err.strip()[:120]!r}")
        else:
            print(f"✓ {name}: rc={rc}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
