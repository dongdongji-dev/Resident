#!/usr/bin/env python3
"""verify_self_send —— 判定某条「以本账号名义发出的消息」是否真的由本 adapter 发出

判据：本 adapter 自己发送的消息，在 adapter.log 里会留下发送链
    event arrived post=message_sent → submitted … deduped=True
    → sent group conv=… → hook action executed
缺了后两行 ⇒ 不是本 adapter 发的（＝同账号的另一个登录端）。

用法：
    python3 verify_self_send.py 1000000001 1000000002
    python3 verify_self_send.py --log /path/adapter.log --window 12 1000000001
输出：每条一行结论 + 命中的日志行摘录（parseable 的纯文本）。
"""
import argparse

DEFAULT_LOG = "/home/sandbox/skill_data/qq_adapter/adapter.log"
SENT_MARKERS = ("sent group conv=", "sent private conv=", "hook action executed")


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def check(lines, msg_id, window):
    hits = [i for i, l in enumerate(lines) if msg_id in l]
    if not hits:
        return "not-found", [], []
    detail, paired_at = [], []
    for i in hits:
        seg = lines[i:i + window]
        detail.extend(f"{i + 1}: {l}" for l in seg)
        if any(any(m in l for m in SENT_MARKERS) for l in seg):
            paired_at.append(i + 1)
    verdict = "ours" if paired_at else "not-ours"
    return verdict, detail, paired_at


def main():
    ap = argparse.ArgumentParser(description="判定 self 消息是否由本 adapter 发出")
    ap.add_argument("msg_ids", nargs="+", help="消息 id（remote_message_id / message_id）")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--window", type=int, default=10, help="命中行之后查看多少行")
    ap.add_argument("--show", action="store_true", help="打印命中的日志行")
    args = ap.parse_args()

    lines = load(args.log)
    for mid in args.msg_ids:
        verdict, detail, paired = check(lines, mid, args.window)
        label = {"ours": "由本 adapter 发出", "not-ours": "非本 adapter 发出（疑似外部登录端）",
                 "not-found": "日志中未找到"}[verdict]
        print(f"{mid}: {label}" + (f"（配对行 {paired}）" if paired else ""))
        if args.show:
            for d in detail:
                print("   ", d)


if __name__ == "__main__":
    main()
