#!/usr/bin/env python3
r"""qq_op.py 会话键入口校验回归测试（与 send_msg.py 同一套规则，2026-10-04）。

只测参数解析层；除「attention set <坏键>」外全部走 `--dry-run`，不产生真实发送/状态变更。
运行：python3 .skills/qq_adapter/tests/test_qq_op_args.py
"""
from __future__ import annotations

import subprocess
import sys

QQ_OP = "/home/sandbox/scripts/qq_op.py"


def run(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run([sys.executable, QQ_OP, *args], input=stdin,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


# (名称, argv, stdin, 期望码, stderr 子串或 None)
CASES = [
    ("send 选项当会话名", ["send", "--to", "1000000002"], None, 2, "应是会话键"),
    ("send 纯数字会话名", ["send", "12345", "hi"], None, 2, "会话键格式可疑"),
    ("send 未知选项", ["send", "private_1000000002", "hi", "--file", "x"], None, 2, "未知选项"),
    ("send 正常 dry-run", ["send", "private_1000000002", "hi", "--dry-run"], None, 0, None),
    ("send 正常+at dry-run", ["send", "group_200000001", "hi", "--at", "1000000002", "--dry-run"], None, 0, None),
    ("send stdin dry-run", ["send", "private_1000000002", "-", "--dry-run"], "多行\n正文", 0, None),
    ("send-file 坏键", ["send-file", "12345", "/tmp/x"], None, 2, "会话键格式可疑"),
    ("send-file 正常 dry-run", ["send-file", "private_1000000002", "/tmp/不存在", "--dry-run"], None, 0, None),
    ("send-image 选项当键", ["send-image", "--to", "/tmp/x"], None, 2, "应是会话键"),
    ("attention set 坏键", ["attention", "set", "12345"], None, 2, "会话键格式可疑"),
    ("缺参数", ["send", "private_1000000002"], None, 1, None),
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
