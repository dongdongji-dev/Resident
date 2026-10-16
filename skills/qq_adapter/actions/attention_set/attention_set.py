#!/usr/bin/env python3
"""attention_set — 设置或关闭注意力群。

用法：
  python3 attention_set.py group_200000001     # 这个群所有消息都唤醒我
  python3 attention_set.py off                 # 关掉（回到：只私聊/@/reply 唤醒）

⚠️ 关掉必须写 off（写成 conversation_id="" 也行），别写成 set off。
副作用提醒：给某个群发过消息后 10 分钟内会自动跟随该群为注意力群；
群里刷屏时怕烧上下文，及时用这个关掉。
状态转移（开/关/切换）会触发一条 core notice 通知（含影响与操作方法）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="设置/关闭注意力群")
    parser.add_argument("conversation",
                        help="group_xxx，或 off 表示关闭")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conversation = "" if args.conversation.lower() == "off" else args.conversation
    if conversation and not conversation.startswith("group_"):
        print("✗ 注意力只支持群会话（group_xxx），关闭写 off", file=sys.stderr)
        return 2

    call([element("set_attention", {"conversation_id": conversation})],
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
