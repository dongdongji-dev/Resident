#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：verify_self_send.py 的判据收紧（2026-10-17）

背景（实测事故）：旧版判据＝「目标 id 命中的行之后 window 行内出现任意 sent 行 ⇒ ours」。
两端（本 adapter 与另一个登录端）密集交错发言时，10 行窗口里会混进**别的消息**的
`sent group … msg_id=` 行，于是把另一端的消息误判成 ours。
实例：1100000001「就是docker不好装」、1100000002「你把docker解决了就没事了」被误判 ours，
只因紧随其后的 1100000003（真·本端发出）的 sent 行落在窗口内。

收紧后的判据：必须【同一行里既有 `sent group/private conv=`、又有该 msg_id】。

跑法：python3 test_verify_self_send.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "tools", "verify_self_send.py"))

FAKE_LOG = """\
2026-10-17 13:49:21,484 [MainThread] INFO qq_adapter.events: event arrived post=message_sent notice= sub=normal msg_type=group req=
2026-10-17 13:49:21,506 [MainThread] INFO qq_adapter.message: submitted normal key=1100000005 deduped=None
2026-10-17 13:49:29,390 [MainThread] INFO qq_adapter.events: event arrived post=message_sent notice= sub=normal msg_type=group req=
2026-10-17 13:49:29,396 [MainThread] INFO qq_adapter.message: submitted normal key=1100000001 deduped=None
2026-10-17 13:49:34,453 [MainThread] INFO qq_adapter.events: event arrived post=message_sent notice= sub=normal msg_type=group req=
2026-10-17 13:49:34,460 [MainThread] INFO qq_adapter.message: submitted normal key=1100000002 deduped=None
2026-10-17 13:49:35,100 [MainThread] INFO qq_adapter.message: submitted normal key=1100000003 deduped=None
2026-10-17 13:49:35,100 [MainThread] INFO qq_adapter.actions: sent group conv=group_200000003 msg_id=1100000003
2026-10-17 13:49:35,119 [MainThread] INFO qq_adapter.callback: hook action executed platform=None ok=True
2026-10-17 13:51:00,000 [MainThread] INFO qq_adapter.message: submitted normal key=1100000004 deduped=None
2026-10-17 13:51:00,001 [MainThread] INFO qq_adapter.actions: sent private conv=private_1000000002 msg_id=1100000004
"""

CASES = [
    ("1100000005", "not-ours", "另一端：仅 message_sent 事件 + submitted，无 sent 行"),
    ("1100000001", "not-ours", "回归·曾被误判 ours（窗口内混进 1100000003 的 sent 行）"),
    ("1100000002", "not-ours", "回归·同上"),
    ("1100000003", "ours", "本端真发出：sent group 行含同一 msg_id"),
    ("1100000004", "ours", "本端私聊发送：sent private 行含同一 msg_id"),
    ("999999999", "not-found", "日志中不存在的 id"),
]


def main():
    failures = 0
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(FAKE_LOG)
        path = f.name
    try:
        for mid, want, note in CASES:
            r = subprocess.run([sys.executable, TOOL, "--log", path, mid],
                               capture_output=True, text=True)
            out = r.stdout.strip()
            if want == "ours":
                got = "ours" if "由本 adapter 发出" in out else "not-ours"
            elif want == "not-ours":
                got = "not-ours" if "非本 adapter 发出" in out else "ours"
            else:
                got = "not-found" if "日志中未找到" in out else "other"
            ok = got == want
            failures += 0 if ok else 1
            print(f"[{'PASS' if ok else 'FAIL'}] {mid}: got={got} want={want}  # {note}")
            if not ok:
                print("   ", out)
    finally:
        os.unlink(path)
    print(f"\n{len(CASES) - failures}/{len(CASES)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
