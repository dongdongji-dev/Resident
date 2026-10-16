#!/usr/bin/env python3
r"""_receipt.norm() 前缀剥离回归测试。

背景（2026-10-13 00:16 实测踩坑）：旧 norm() 见开头是 `[` 就把整段括号内容
当适配器前缀剥掉——我发的正文以 `[自测]` 开头，被剥成「验证注意力自动跟随
通知…」→ 回执误报「⚠️ 回执不一致」。修法＝只剥适配器**真的会加**的前缀。

运行：python3 .skills/qq_adapter/tests/test_receipt_norm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))

import _receipt  # noqa: E402

CASES = [
    # (落库内容, 期望 norm 结果, 说明)
    ("[reply]你好", "你好", "旧：适配器加的 [reply] 前缀要剥"),
    ("[at 1000000002] 你好", "你好", "旧：@ 前缀要剥"),
    ('<reply msg_id="123"/>你好', "你好", "旧：<reply/> 段前缀要剥"),
    ('&lt;reply msg_id="1"/&gt;hi', "hi", "旧：HTML 转义 + 段前缀"),
    ("[notice 系统] x", "x", "旧：系统前缀"),
    # 新（回归点）：正文自己以 [..] 开头不能被剥
    ("[自测] 验证注意力自动跟随通知，稍后关闭。",
     "[自测] 验证注意力自动跟随通知，稍后关闭。", "新：正文自带方括号前缀要保留"),
    ("[重要]记得吃饭", "[重要]记得吃饭", "新：同上"),
    ("【中文括号】不动", "【中文括号】不动", "新：中文括号本来就不该动"),
    ("普通正文", "普通正文", "无前缀原样"),
    ("", "", "空串"),
]


def main() -> int:
    bad = 0
    for raw, want, why in CASES:
        got = _receipt.norm(raw)
        if got == want:
            print(f"✓ {why}")
        else:
            bad += 1
            print(f"✗ {why}: norm({raw!r}) = {got!r}（期望 {want!r}）")
    # 前缀剥离要能连剥两段（[reply][at x]）
    got = _receipt.norm("[reply][at 123] 内容")
    if got == "内容":
        print("✓ 连续两段前缀都能剥")
    else:
        bad += 1
        print(f"✗ 连续两段前缀：得 {got!r}")
    print(f"\n{len(CASES) + 1 - bad}/{len(CASES) + 1} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
