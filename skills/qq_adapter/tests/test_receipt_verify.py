#!/usr/bin/env python3
r"""_receipt.verify() 回归测试（同号另一登录端活跃时的「回执误报」）。

背景：同一账号存在另一个登录端时，库里 out 序列里混着**不是本端**的消息；
旧的 verify() 只做「逐字/前缀内容比对」，一旦对方在本端那条消息之后又发了别的
out，最新 out 就对不上 —— 误报「⚠️ 回执不一致」（消息其实发对了）。

修法：**先按 message_id（库里的 remote_message_id）配对**，命中即通过；
内容比对退为兜底；两者都不中时在告警里附 `verify_self_send.py <msg_id>` 复核提示。

运行：python3 .skills/qq_adapter/tests/test_receipt_verify.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))

import _receipt  # noqa: E402

CONV = "private_1000000002"


def _msg(seq, mid, content, direction="out"):
    return {"seq": seq, "remote_message_id": mid, "content": content,
            "direction": direction}


def _run(msgs, text, response, *, saved=None):
    """把 _api_messages 等外部依赖打桩，跑 verify()。"""
    _receipt._load_seq = lambda conv: 100          # 不碰磁盘
    _receipt._save_seq = lambda conv, seq: saved.append(seq) if saved is not None else None
    _receipt._api_messages = lambda conv, before, limit=300: msgs
    return _receipt.verify(CONV, text, response, tries=1)


def main() -> int:
    bad = 0

    def check(cond, why):
        nonlocal bad
        if cond:
            print(f"✓ {why}")
        else:
            bad += 1
            print(f"✗ {why}")

    # ① 另一端占位：最新 out 是另一端的，我的那条按 message_id 命中 ⇒ 必须通过
    msgs = [
        _msg(101, "900000001", "我都批进来了"),
        _msg(102, "900000123", "这条是本端发出的正文。"),
        _msg(103, "900000002", "都没批进来"),
    ]
    out = _run(msgs, "这条是本端发出的正文。",
               {"result": [{"message_id": 900000123}]})
    check("回执对账通过" in out and "不一致" not in out,
          "另一端占位时按 message_id 配对，不再误报不一致")
    check("seq 102" in out, "命中行带库内 seq")

    # ② 没有 message_id 命中，但内容逐字一致 ⇒ 通过（旧路径保留）
    msgs2 = [_msg(104, "900000003", "别的"), _msg(105, "900000004", "内容逐字一致")]
    out = _run(msgs2, "内容逐字一致", {"result": [{"message_id": 555}]})
    check("回执对账通过" in out and "不一致" not in out, "无 id 命中时内容比对仍生效")

    # ③ 前缀命中（适配器加了 [reply] 段）⇒ 通过
    msgs3 = [_msg(106, "900000005", '[reply]<reply msg_id="1"/>正文前缀一致，后面还有内容')]
    out = _run(msgs3, "正文前缀一致，后面还有内容", {"result": [{"message_id": 556}]})
    check("回执对账通过" in out, "剥前缀后内容比对通过")

    # ④ 都没中 ⇒ 报不一致，并附 verify_self_send 复核提示
    msgs4 = [_msg(107, "900000006", "无关的另一端消息")]
    out = _run(msgs4, "我这条根本没落库", {"result": [{"message_id": 900000123}]})
    check("不一致" in out, "真对不上时仍报不一致")
    check("verify_self_send.py 900000123" in out, "告警附复核提示")

    # ⑤ message_id 为空（真·静默失败）⇒ 仍走「可疑」+ 不一致，不因新逻辑变绿
    msgs5 = [_msg(108, "900000007", "另一端")]
    out = _run(msgs5, "没发出去", {"result": [{"message_id": None}]})
    check("回执可疑" in out, "message_id 空仍报可疑（不误判为通过）")

    # ⑥ 我发的正文以 [自测] 开头（历史误报点）⇒ 逐字一致要判通过
    msgs6 = [_msg(109, "900000008", "[自测] 验证注意力自动跟随通知，稍后关闭。")]
    out = _run(msgs6, "[自测] 验证注意力自动跟随通知，稍后关闭。", {"result": [{"message_id": 557}]})
    check("回执对账通过" in out, "正文自带方括号前缀不再被误剥")

    total = 8
    print(f"\n{total - bad}/{total} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
