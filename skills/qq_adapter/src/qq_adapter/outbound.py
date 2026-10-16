"""自己发出的消息登记（内存 LRU）：message_sent 回显去重 + 引用/撤回识别。

背景（2026-09-24 00:40 发现并修复）：一条自己发出的消息有两条入库路径——

1. ``/hook`` 出站上报（actions._report_out）：会话正确（private_<对方>）、即时；
2. NapCat 的 ``message_sent`` 回显（handlers._handle_message）：旧实现一律用
   ``user_id`` 解析会话，而回显里 ``user_id`` 是 bot 自己（真正的对方在
   ``target_id``）→ 消息被写进 ``private_<bot 自己>`` 这个假会话，与 1 重复
   （同 remote_message_id 两条）。

去重靠这里登记的 message_id：出站上报时 ``register()``，回显处理时 ``known()``
命中就直接丢弃。顺带存一条预览文本，供「被引用/撤回的是不是我发的」判断用
（原先走 ``submitter.get_by_remote``，但 core 没有该端点——``/api/messages/
by-remote`` 实测 404，整条判断链是哑的）。

进程重启会丢登记，此时回显会走到会话解析分支：解析出的会话与 1 一致，
core 按 conversation_key 去重兜住（2026-09-24 实测）。"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_MAXLEN = 1000
PREVIEW_CHARS = 200


class OutboundRegistry:
    """最近发出的 message_id → 预览文本（OrderedDict 做 LRU，超出丢最旧）。"""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._maxlen = max(1, int(maxlen))
        self._items: OrderedDict[str, str] = OrderedDict()

    def register(self, message_id: object, preview: str = "") -> None:
        key = str(message_id or "").strip()
        if not key:
            return
        self._items.pop(key, None)
        self._items[key] = str(preview or "")[:PREVIEW_CHARS]
        while len(self._items) > self._maxlen:
            self._items.popitem(last=False)

    def known(self, message_id: object) -> bool:
        return str(message_id or "").strip() in self._items

    def preview(self, message_id: object) -> str:
        return self._items.get(str(message_id or "").strip(), "")

    def __len__(self) -> int:
        return len(self._items)


OUTBOUND = OutboundRegistry()


def register(message_id: object, preview: str = "") -> None:
    OUTBOUND.register(message_id, preview)


def known(message_id: object) -> bool:
    return OUTBOUND.known(message_id)


def preview(message_id: object) -> str:
    return OUTBOUND.preview(message_id)
