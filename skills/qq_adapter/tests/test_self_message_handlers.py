"""qq_adapter 自己发出消息（message_sent 回显）的处理：会话解析 + 去重 + 引用识别。

2026-09-24 修复回归测试。背景：NapCat 的 message_sent 事件里 user_id 是 bot
自己，真正的对方在 target_id；旧实现一律用 user_id → 自己发的私聊消息被写进
`private_<bot 自己>` 假会话，且与 /hook 出站上报重复（同 remote_message_id 两条）。
去重靠 qq_adapter.outbound 内存登记（core 没有 by-remote 端点，实测 404）。

跑法：cd .skills/qq_adapter && python3 -m unittest tests.test_self_message_handlers -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_adapter import handlers, outbound  # noqa: E402
from qq_adapter.events import EventContext  # noqa: E402


class _FakeSubmitter:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, draft):
        self.submitted.append(draft)
        return {"ok": True}


def _ctx(submitter=None, self_id="1000000001") -> EventContext:
    return EventContext(submitter=submitter or _FakeSubmitter(),
                        self_id=self_id, platform="qq[1000000001]")


def _self_private_event(**over) -> dict:
    event = {
        "post_type": "message_sent",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": "66123032",
        "user_id": 1000000001,      # NapCat 回显里＝bot 自己
        "target_id": 1000000002,     # 真正的对方
        "self_id": 1000000001,
        "message_sent_type": "self",
        "sender": {"user_id": 1000000001, "nickname": "该 agent"},
        "raw_message": "hei",
        "message": [{"type": "text", "data": {"text": "hei"}}],
        "time": 1758647217,
    }
    event.update(over)
    return event


class _ClearRegistry(unittest.TestCase):
    def setUp(self):
        outbound.OUTBOUND._items.clear()


class PrivateConversationIdTest(_ClearRegistry):
    def test_self_message_uses_target_id(self):
        ctx = _ctx()
        self.assertEqual(
            handlers._private_conversation_id(_self_private_event(), True, ctx),
            "private_1000000002")

    def test_self_message_falls_back_to_user_id_when_target_is_self(self):
        """若某天 NapCat 改成 user_id=对方、target_id=自己，也要取对。"""
        ctx = _ctx()
        event = _self_private_event(user_id=1000000002, target_id=1000000001)
        self.assertEqual(
            handlers._private_conversation_id(event, True, ctx),
            "private_1000000002")

    def test_self_message_without_peer_is_dropped(self):
        ctx = _ctx()
        event = _self_private_event(target_id=1000000001, user_id=1000000001)
        self.assertEqual(handlers._private_conversation_id(event, True, ctx), "")

    def test_inbound_message_uses_user_id(self):
        ctx = _ctx()
        event = _self_private_event(post_type="message", user_id=1000000002,
                                    target_id=None)
        self.assertEqual(
            handlers._private_conversation_id(event, False, ctx),
            "private_1000000002")


class HandleMessageTest(_ClearRegistry):
    def _run(self, event, ctx):
        return asyncio.run(handlers._handle_message(event, ctx))

    def test_duplicate_self_message_is_skipped(self):
        outbound.register("66123032", "hei")   # /hook 出站上报已登记
        draft = self._run(_self_private_event(), _ctx())
        self.assertIsNone(draft)

    def test_self_message_submitted_when_not_registered(self):
        """adapter 重启窗口 / napcat 侧直发：登记表里没有 → 入库，会话也正确。"""
        draft = self._run(_self_private_event(), _ctx())
        self.assertIsNotNone(draft)
        self.assertEqual(draft.conversation_id, "private_1000000002")
        self.assertEqual(draft.direction, "out")

    def test_self_message_without_peer_dropped(self):
        """拿不到对方 → 丢弃，绝不写 private_<bot 自己> 假会话。"""
        draft = self._run(
            _self_private_event(user_id=1000000001, target_id=1000000001),
            _ctx())
        self.assertIsNone(draft)

    def test_group_self_message_unchanged(self):
        event = _self_private_event(message_type="group", group_id=200000001)
        draft = self._run(event, _ctx())
        self.assertEqual(draft.conversation_id, "group_200000001")

    def test_inbound_private_unchanged(self):
        event = _self_private_event(post_type="message", user_id=1000000002)
        draft = self._run(event, _ctx())
        self.assertEqual(draft.conversation_id, "private_1000000002")
        self.assertEqual(draft.direction, "in")


class OutboundRegistryTest(_ClearRegistry):
    def test_lru_eviction(self):
        reg = outbound.OutboundRegistry(maxlen=3)
        for i in range(4):
            reg.register(str(i))
        self.assertFalse(reg.known("0"))
        self.assertTrue(reg.known("3"))

    def test_preview_truncated(self):
        reg = outbound.OutboundRegistry()
        reg.register("1", "x" * 500)
        self.assertEqual(len(reg.preview("1")), outbound.PREVIEW_CHARS)

    def test_blank_id_ignored(self):
        reg = outbound.OutboundRegistry()
        reg.register("", "y")
        reg.register(None)
        self.assertEqual(len(reg), 0)

    def test_is_bot_message_uses_registry(self):
        ctx = _ctx()
        self.assertFalse(handlers._is_bot_message(ctx, "42"))
        outbound.register("42", "我发的")
        self.assertTrue(handlers._is_bot_message(ctx, "42"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
