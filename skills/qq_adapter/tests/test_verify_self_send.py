#!/usr/bin/env python3
r"""verify_self_send.py 回归测试（2026-10-15，账号安全取证工具）。

只测纯函数 check()：给定日志行 + 消息 id，判定是否是本 adapter 发出的。
运行：python3 .skills/qq_adapter/tests/test_verify_self_send.py
"""
from __future__ import annotations

import importlib.util
import sys

TOOL = "/home/sandbox/.skills/qq_adapter/tools/verify_self_send.py"

# 真实日志形态的样本（消息 id 为示例值）
LOG = [
    # —— 本 adapter 发出的（有完整发送链）——
    "17:43:57,247 [MainThread] INFO qq_adapter.events: event arrived post=message_sent notice= sub=normal",
    "17:43:57,273 [MainThread] INFO qq_adapter.message: submitted normal key=1000000001 deduped=None",
    "17:43:57,281 [MainThread] INFO qq_adapter.message: submitted normal key=1000000001 deduped=True",
    "17:43:57,281 [MainThread] INFO qq_adapter.actions: sent group conv=group_200000004 msg_id=1000000001",
    "17:43:57,282 [MainThread] INFO qq_adapter.callback: hook action executed platform=None ok=True",
    "17:43:59,302 [MainThread] INFO qq_adapter.events: event arrived post=message notice= sub=normal",
    # —— 夺舍那条（只有事件 + submitted，无发送链）——
    "17:44:19,917 [MainThread] INFO qq_adapter.events: event arrived post=message_sent notice= sub=normal",
    "17:44:19,997 [MainThread] INFO qq_adapter.message: submitted normal key=1000000002 deduped=None",
    "17:44:20,547 [MainThread] INFO qq_adapter.events: event arrived post=meta_event",
]


def load_tool():
    spec = importlib.util.spec_from_file_location("verify_self_send", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    t = load_tool()
    cases = [
        ("1000000001", "ours", "有四行发送链 ⇒ 本 adapter 发出"),
        ("1000000002", "not-ours", "只有事件+submitted ⇒ 非本 adapter 发出"),
        ("999999999", "not-found", "日志里没有 ⇒ not-found"),
    ]
    ok = True
    for mid, want, why in cases:
        verdict, detail, paired = t.check(LOG, mid, 10)
        good = verdict == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'}  {mid}: {verdict}（期望 {want}）— {why}")
    # 窗口边界：window=0 时不看命中行之后 ⇒ 一定判 not-ours（说明 window 真的在用）
    v2, _, _ = t.check(LOG, "1000000001", 0)
    print(f"INFO  window=0 时 1000000001 判定={v2}（预期 not-ours，说明窗口参数生效）")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
