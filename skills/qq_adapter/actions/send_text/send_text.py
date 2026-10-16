#!/usr/bin/env python3
"""send_text — 往一个会话发一条纯文本消息。

用法：
  python3 send_text.py group_200000001 "大家好"
  python3 send_text.py private_1000000002 "在的" --reply 123456   # 引用回复
  python3 send_text.py group_200000001 "看这里" --at 1000000002   # @ 某人（自动补空格）
  python3 send_text.py group_200000001 "紧急" --force            # 跳过未读仲裁
  echo "多行" | python3 send_text.py group_200000001 -           # 从 stdin 读正文
  python3 send_text.py group_200000001 --from-file /tmp/body.txt # 正文取自文件（推荐）
  python3 send_text.py group_200000001 "试" --dry-run            # 只看 payload 不发
  python3 send_text.py group_200000001 "不用核对" --no-receipt    # 跳过回执对账
  python3 send_text.py --conversation group_200000001 --text "具名写法"  # 等价（防呆别名，2026-10-15 加）

会话键 = group_xxx / private_xxx（也认 bili_private_xxx 这类带前缀的）。
默认发送后会做**回执对账**（经 core API 读消息库那条 out 记录逐字核对）；`--no-receipt` 跳过。

⚠️ 转义：正文含反引号 `` ` ``、`$`、`!`、`\\` 等 shell 元字符时**别用双引号内联**——
   shell 会先做命令替换/变量展开，内容被吞掉（2026-09-26、2026-10-03 各踩一次）。
   三种安全写法：① 单引号 `'...'` ② `-` 走 stdin ③ **`--from-file <路径>`（正文完全不过 shell，最稳）**。
   `--from-file` 与正文参数二选一（给了文件就不用再写正文）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _hook import call, element, segment, text_segment  # noqa: E402
from _receipt import verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发纯文本消息")
    parser.add_argument("conversation", nargs="?",
                        help="会话键：group_xxx / private_xxx（也可用 --conversation）")
    parser.add_argument("text", nargs="?",
                        help="正文；写 - 表示从 stdin 读；用 --from-file 时省略（也可用 --text）")
    # 防呆别名（2026-10-15）：本脚本的会话键/正文是位置参数，用 --conversation/--text
    # 具名写法会直接报 unrecognized arguments，agent 已踩过一次；这里接受两者。
    parser.add_argument("--conversation", dest="conversation_opt",
                        help="[防呆别名] 等价于位置参数 conversation")
    parser.add_argument("--text", dest="text_opt",
                        help="[防呆别名] 等价于位置参数 text")
    parser.add_argument("--from-file", dest="from_file",
                        help="正文取自该文件的全部内容（不走 shell，元字符安全）")
    parser.add_argument("--reply", help="引用回复的消息 id")
    parser.add_argument("--at", dest="at_id", help="要 @ 的 QQ 号")
    parser.add_argument("--force", action="store_true",
                        help="跳过未读仲裁（force_exec=true）")
    parser.add_argument("--no-receipt", action="store_true",
                        help="不做回执对账（默认做）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # 把两个防呆别名归一化回位置参数（同时给了且不一致 → 直接拒绝，别猜）
    if args.conversation_opt is not None:
        if args.conversation is not None and args.conversation != args.conversation_opt:
            print("✗ 会话键给了两次且不一致", file=sys.stderr)
            return 2
        args.conversation = args.conversation_opt
    if args.text_opt is not None:
        if args.text is not None and args.text != args.text_opt:
            print("✗ 正文给了两次且不一致", file=sys.stderr)
            return 2
        args.text = args.text_opt
    if not args.conversation:
        print("✗ 要会话键：位置参数 group_xxx / private_xxx，或 --conversation <键>",
              file=sys.stderr)
        return 2

    if args.from_file:
        path = Path(args.from_file)
        if not path.is_file():
            print(f"✗ 文件不存在：{path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8")
    elif args.text is None:
        print("✗ 要正文：写第 2 个参数、用 `-` 走 stdin，或给 --from-file <路径>",
              file=sys.stderr)
        return 2
    elif args.text == "-":
        text = sys.stdin.read()
    else:
        text = args.text

    content = []
    if args.reply:
        content.append(segment("reply", {"msg_id": args.reply}))
    content.append(text_segment(text))
    if args.at_id:
        content.append(segment("at", {"target_id": args.at_id}))

    attributes = {"conversation_id": args.conversation}
    if args.force:
        attributes["force_exec"] = "true"

    response = call([element("send_messages", attributes, content)],
                    dry_run=args.dry_run)
    if not args.dry_run and not args.no_receipt:
        print(verify(args.conversation, text, response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
