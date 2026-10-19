"""Attention group state: runtime mutable + JSON persisted.

The agent switches its "attention group" over HTTP (set_attention /
get_attention hook actions); group messages in the attention group
always wakeup the agent (no need to @). Only one group at a time.

Auto-follow + idle expiry (2026-09-01 方案，已实现):
- follow(group_id): 向某群发送消息后自动把注意力切到该群（含刷新时间戳）；
- touch(group_id):  对当前注意力群执行了任何成功操作 → 刷新活跃时间戳
                     （在 ActionExecutor 出站分派成功后统一调用）；
- 闲置自动过期: 超过 IDLE_TIMEOUT_SECONDS（默认 120 秒＝2 分钟）未对注意力群执行
                  任何操作 → 注意力自动关闭；检测时机 = 接收消息时
                  惰性检查（handlers.py _wakeup_for 每次收消息都会
                  调 ctx.attention.get()，正好是过期检测点）。

变更通知（2026-10-12 方案，已实现）：
- 注意力**状态发生转移**时（开→群 / 群→群 / 群→关），调用注入的
  notifier(reason, previous, current) 产生一条通知（`qq_self` 会话、
  message_type=attention、wakeup=True），告知：哪个会话、什么原因、
  影响是什么、怎么开/关/查。
- 只报**真实转移**：follow 到同一个群（只是刷新时间戳）不报，
  touch 不报，重启恢复状态不报——避免刷屏。
- reason 取值：manual（我显式 set）/ follow（发消息自动跟随）/
  expire（闲置超时自动关闭）。

State lives in a small JSON file (default run/attention.json) so it
survives adapter restarts; NOT in the frozen dataclass config.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("qq_adapter.attention")

# 闲置超时：超过该时长未对注意力群执行任何操作 → 自动关闭注意力（秒）
# 2026-10-18 定为 120 秒（2 分钟）；原 600 太长。可用环境变量覆盖。
IDLE_TIMEOUT_SECONDS = int(os.environ.get("QQ_ATTENTION_IDLE_SECONDS", "120"))

# notifier 签名：(reason, previous_group, current_group) -> None
Notifier = Callable[[str, str, str], None]

_REASON_LABEL = {
    "manual": "手动设置",
    "follow": "发消息后自动跟随",
    "expire": "闲置超时自动关闭",
}


def render_attention_notice(reason: str, previous: str, current: str) -> str:
    """把一次注意力变更渲染成给人看的一段通知文本。

    内容三要素（2026-10-12 约定）：哪个会话变了 / 影响是什么 /
    怎么开、关、查。
    """
    label = _REASON_LABEL.get(reason, reason)
    if current and not previous:
        title = f"注意力已开启：{current}"
    elif current and previous:
        title = f"注意力已切换：{previous} → {current}"
    else:
        title = f"注意力已关闭（原：{previous or '无'}）"
    lines = [
        f"[注意力] {title}（{label}）",
        "影响：注意力群内所有新消息都会唤醒我（无需 @ 或 reply）；"
        "同时只能有一个注意力群。关闭后回到：仅私聊 / @我 / 回复我 才唤醒。",
        "操作：开启或切换 = attention_set <group_xxx>；"
        "关闭 = attention_set off；查看当前 = attention_get。",
    ]
    if reason == "follow":
        lines.append("（这是给该群发消息后自动跟随的结果，非我显式设置。）")
    elif reason == "expire":
        lines.append("（闲置超 10 分钟未对该群做任何操作，自动过期。）")
    return "\n".join(lines)


class AttentionStore:
    """Thread-safe attention group holder with JSON persistence.

    - set(group_id)   : switch attention to another group (empty = off)
    - follow(group_id): auto-follow on outbound group send (set + refresh)
    - touch(group_id) : refresh last_active if group is the current one
    - get()           : current attention group id ("" if none); lazily
                        expires the group if idle too long
    - info()          : current group + idle/expiry info (get_attention)
    - on restart the persisted value is restored automatically.

    可选 notifier：状态发生**真实转移**时回调（见模块 docstring）。
    """

    def __init__(self, path: str = "run/attention.json",
                 idle_timeout: int = IDLE_TIMEOUT_SECONDS,
                 notifier: Optional[Notifier] = None) -> None:
        self._path = Path(path)
        self._idle_timeout = int(idle_timeout)
        self._notifier = notifier
        self._lock = threading.RLock()
        self._group_id: str = ""
        self._last_active: float = 0.0
        self._load()

    # ── 持久化 ────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._group_id = str(raw.get("group_id", "") or "")
                self._last_active = float(raw.get("last_active", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            logger.warning("attention state load failed path=%s", self._path,
                           exc_info=True)
            self._group_id = ""
            self._last_active = 0.0

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"group_id": self._group_id,
                            "last_active": self._last_active},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("attention state save failed path=%s",
                           self._path, exc_info=True)

    # ── 通知 ──────────────────────────────────────────────────────
    def _notify(self, reason: str, previous: str, current: str) -> None:
        """调用 notifier（异常只记日志，绝不影响注意力状态机本身）。"""
        if self._notifier is None:
            return
        try:
            self._notifier(reason, previous, current)
        except Exception:  # noqa: BLE001
            logger.warning("attention notifier failed reason=%s %r->%r",
                           reason, previous, current, exc_info=True)

    # ── 闲置过期 ──────────────────────────────────────────────────
    def _is_idle(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self._group_id:
            return False
        if self._last_active <= 0:
            # 旧状态无时间戳（如 08-31 手工清空遗留）→ 不误杀，等下次
            # set/follow 写入新时间戳
            return False
        return (now - self._last_active) > self._idle_timeout

    def _expire_if_idle(self) -> Optional[str]:
        """闲置则清空注意力；返回被清掉的群（未过期返回 None）。
        通知在锁外发出（notifier 内可能是一次 HTTP POST）。"""
        with self._lock:
            if not self._is_idle():
                return None
            expired = self._group_id
            self._group_id = ""
            self._last_active = 0.0
            self._save()
        logger.info("attention auto-expired (idle > %ss): %r",
                    self._idle_timeout, expired)
        return expired

    # ── 读写 ──────────────────────────────────────────────────────
    def get(self) -> str:
        expired = self._expire_if_idle()
        if expired:
            self._notify("expire", expired, "")
        with self._lock:
            return self._group_id

    def set(self, group_id: str, reason: str = "manual") -> str:
        """切换注意力群（''=关闭）。返回切换前的群（供确认）。
        仅当状态发生真实转移时发通知（同一群重复 set 不报）。"""
        group_id = str(group_id or "").strip()
        with self._lock:
            previous = self._group_id
            if group_id:
                # 显式切换群/开启 → 刷新活跃时间戳（此刻起算 10 分钟）
                self._last_active = time.time()
            else:
                self._last_active = 0.0
            self._group_id = group_id
            self._save()
        logger.info("attention group switched %r -> %r (%s)",
                    previous, group_id, reason)
        if previous != group_id:
            self._notify(reason, previous, group_id)
        return previous

    def follow(self, group_id: str) -> str:
        """发送消息自动跟随：把注意力切到目标群并刷新时间戳。
        等价 set(reason='follow')（含活跃时间刷新），语义上用于出站发送成功后自动关注。"""
        return self.set(group_id, reason="follow")

    def touch(self, group_id: str) -> None:
        """对当前注意力群执行了操作 → 刷新活跃时间戳。
        非当前注意力群的操作不改状态（不影响现有注意力）。"""
        group_id = str(group_id or "").strip()
        with self._lock:
            if not group_id or not self._group_id:
                return
            if group_id == self._group_id:
                self._last_active = time.time()
                self._save()

    def info(self) -> dict:
        """当前注意力状态（含闲置剩余秒数），供 get_attention 展示。"""
        expired = self._expire_if_idle()
        if expired:
            self._notify("expire", expired, "")
        with self._lock:
            now = time.time()
            remaining = 0
            if self._group_id and self._last_active > 0:
                remaining = int(self._idle_timeout
                                - (now - self._last_active))
                if remaining < 0:
                    remaining = 0
            return {
                "conversation_id": self._group_id,
                "last_active": self._last_active,
                "idle_timeout_seconds": self._idle_timeout,
                "expires_in_seconds": remaining,
            }


# 单例（adapter 进程内共享：ActionExecutor + EventContext 同源）
_store: Optional[AttentionStore] = None


def get_store(path: str = "run/attention.json") -> AttentionStore:
    global _store
    if _store is None:
        _store = AttentionStore(path)
    return _store
