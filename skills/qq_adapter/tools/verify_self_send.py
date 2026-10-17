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
# 只有 actions 模块在真正发出去之后才打这两行（且行内含 msg_id）。
# ⚠️ 不要用 `submitted normal key=<id>` 或 `hook action executed` 当判据：
#   - `submitted …` 在别人用同账号发消息（post=message_sent）时也会打；
#   - `hook action executed` 行里没有 msg_id。
SENT_PATTERNS = ("sent group conv=", "sent private conv=")


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
        # 判据（2026-10-17 收紧）：必须【同一行里既有发送标记、又有该 msg_id】。
        # 旧版「窗口内出现任意 sent 行即判 ours」在两端密集交错时会串行误判
        # （实测：一端的两条被误判 ours，只因 10 行窗口里混进了另一端那条的 sent 行）。
        if any(msg_id in l and any(p in l for p in SENT_PATTERNS) for l in seg):
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
