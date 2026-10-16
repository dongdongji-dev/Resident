#!/usr/bin/env python3
r"""self_message_watch.py 的轮转逻辑回归测试（2026-10-15）。

只测 rotate_if_needed()：临时目录造文件，验证阈值前后行为。
运行：python3 .skills/qq_adapter/tests/test_self_message_watch.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile

TOOL = "/home/sandbox/.skills/qq_adapter/tools/self_message_watch.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("self_message_watch", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    t = load_tool()
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "w.jsonl"
        p.write_bytes(b"x" * 100)

        # 1) 未达上限 ⇒ 不轮转
        r = t.rotate_if_needed(p, 1000)
        good = (r is False) and p.exists()
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  未达上限：rotate={r}，原文件仍在={p.exists()}")

        # 2) 达到上限 ⇒ 轮转为 .1，原文件让位
        r = t.rotate_if_needed(p, 100)
        rolled = pathlib.Path(str(p) + ".1")
        good = (r is True) and rolled.exists() and (not p.exists())
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  达上限：rotate={r}，.1 存在={rolled.exists()}，原文件已让位={not p.exists()}")

        # 3) max_bytes=0 ⇒ 永不轮转
        p.write_bytes(b"y" * 100)
        r = t.rotate_if_needed(p, 0)
        good = (r is False) and p.exists()
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  max=0：rotate={r}")

        # 4) 文件不存在 ⇒ 不报错、不轮转
        missing = pathlib.Path(d) / "nope.jsonl"
        r = t.rotate_if_needed(missing, 10)
        good = (r is False)
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  文件不存在：rotate={r}（应 False，不抛异常）")

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
