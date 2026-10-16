#!/usr/bin/env python3
"""parse_serial_commands —— 串行命令切分的回归测试。

2026-10-14 实测踩坑：旧实现用正则 `\\s*(?:&&|\\|\\||;|\\|)\\s*` 切分，把
`python3 -c '…print(f"{a} | {b}")…'` 里 f-string 的 `|` 也当成管道切开，
碎片被当成命令名 → 白名单误判、狂弹 exec 审批。本测试锁住"引号内不切"。

运行：python3 tests/test_serial_commands.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_framework.workspace_tools import (  # noqa: E402
    _command_name, parse_serial_commands)

CASES: list[tuple[str, list[str]]] = [
    # 单命令
    ("ls -la /home/sandbox", ["ls"]),
    # 引号内的分隔符不切（本次修的就是这条）
    ('python3 -c \'\nimport x\nprint(f"{a} | {b}")\n\'', ["python3"]),
    ('grep -n "x\\|y" f.txt', ["grep"]),
    ('echo "a;b"', ["echo"]),
    ('echo "a && b"', ["echo"]),
    # 顶层分隔符照切
    ("ls /home/x && cat a.txt", ["ls", "cat"]),
    ('echo "a;b" ; ls', ["echo", "ls"]),
    ("cmd 2>&1 | tail -2", ["cmd", "tail"]),
    ("a || b", ["a", "b"]),
    # 多段串接
    ('cd /a && python3 -c "print(1)"; echo done', ["cd", "python3", "echo"]),
    # 空段过滤
    ("ls ; ; pwd", ["ls", "pwd"]),
    # ── heredoc：body 不参与判定（2026-10-14 23:0x 修的第二个坑）────────
    # body 里的每行原本都会被当成一条命令 → 一个 g++ 命令报 30+ 条"不在白名单"
    ("cat > k.cpp <<'EOF'\nint main(){ cout<<1; }\nEOF", ["cat"]),
    # body 之后的内容必须**另起一段**（不能并进 cat 逃过白名单）
    ("cd /w && cat > v.py <<EOF\nfor i in x: print(i)\nEOF\ntimeout 600 python3 v.py",
     ["cd", "cat", "timeout"]),
    # <<- 允许 tab 缩进的定界符
    ("cat <<-EOF\n\tdo\n\t}\n\tEOF\nls", ["cat", "ls"]),
    # 引号里的 << 不是 heredoc
    ('echo "a<<b" && ls', ["echo", "ls"]),
    # C++ 流操作符不是 heredoc（无独立标识符定界）
    ("python3 -c 'print(1<<3)' && ls", ["python3", "ls"]),
    # 未闭合的 heredoc：不崩，body 吃到结尾
    ("cat > a.txt <<EOF\nhello", ["cat"]),
]

FAILS = 0


def main() -> int:
    global FAILS
    for cmd, expect in CASES:
        segs = parse_serial_commands(cmd)
        got = [_command_name(s) for s in segs]
        ok = got == expect
        if not ok:
            FAILS += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {cmd[:46]!r:50s} -> {got}")
    print(f"\n{'ALL PASS' if not FAILS else f'{FAILS} FAILED'}"
          f" ({len(CASES)} cases)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
