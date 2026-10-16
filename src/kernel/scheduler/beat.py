"""Beat: the Kernel's clock / scheduler loop.

Every ``interval_seconds`` tick, in fixed order:

1. wake decision:
   - urgent: unread messages with ``wakeup=1`` exist → fire immediately
     WITHOUT a probability check (the notice channel absorbs it; urgent
     messages must not wait);
   - otherwise the effective probability roll ``p * e^(unread_weight *
     unread)`` (capped 1.0) decides; wake-dedup cooldown and the agent's
     busy state gate the general channel. NOT every unread triggers a wake.
2. scheduled tasks (extensible; currently only message archiving every
   ``archive_interval_hours``; ``last_archive_at`` persisted so a restart
   does not re-run).
3. drain the event queue (kinds are reserved for future extension — only
   logged for now).
4. flush — deliver ALL due notices to the Agent in one batch at the END of
   the tick. The Beat NEVER pushes to the Agent directly; the notice table
   is the single normalized channel.

Wake payload (per session, ``platform:conversation_id``): all unread
messages grouped by session, top ``wake_x_convs`` sessions carried; each
carried session shows the earliest + latest message plus ALL urgent
(wakeup=1) messages, DEDUPED by conversation_key (each key shown once).
Urgent messages get the ``（有人找你）`` prefix. Carried messages are marked
read atomically; urgent messages are DEMOTED (wakeup -> 0) after display so
they never re-trigger the urgent branch while staying unread.

Supplement channel: if a session already has events sitting in the agent
event queue (e.g. the previous wake notice has not been merged yet — the
agent-busy multi-round window), that session switches to a lean supplement
notice: only newly arrived (有人找你) messages, never the earliest message
(repeated wakes would keep dragging it along), and no standard header /
footer. Sessions not in the queue keep the full wake payload.

The probability is maintained by the AGENT thread (tool call -> reset,
no tool call -> halve, persisted in ``system_vars.awake_probability``);
after a wake it is cooled to the floor.
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.content import text_of
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.paths import open_file_inside
from kernel.agent_framework.timeutil import now_str
from kernel.osdb.messages import MessagesStore
from kernel.osdb.notice import NoticeStore

# 运行时消息标识（与 runtime_block 注入说明共用，改这里即两处同步）
URGENT_PREFIX = "（有人找你）"
NOTICE_ENVELOPE = "[通知 #{notice_id} {time}]"
TAIL_LINE_TEMPLATE = ("已展示 {displayed} 条，实际本会话在这个时间段还存在 "
                      "{remaining} 条未读未展示消息，请及时处理")
NOT_CARRIED_HINT = "本次系统暂未携带其消息"
PREVIEW_HEADER = ("各会话最新消息预览（每会话最新一条，含自己发送的消息；"
                  "仅作了解，不需要回复）：")

from kernel.osdb.system_vars import SystemVarsStore
from kernel.osdb.todos import TodoStore

logger = get_logger("kernel.scheduler.beat")


@dataclass(frozen=True, slots=True)
class BeatConfig:
    enabled: bool
    interval_seconds: float
    archive_interval_hours: float
    archive_days: int
    initial_probability: float
    min_probability: float
    # 未读加权唤醒：p * e^(unread_weight * 未读数)
    unread_weight: float
    # 唤醒携带的最大会话数
    wake_x_convs: int
    # 每条消息内容最大字符数
    wake_max_chars: int
    # 每会话展示最新一条（最早 + 会话绝对最新，含己方/已读消息）
    inject_latest: bool
    # 自由活动窗口：是否附带"各会话最新消息预览"
    free_preview_enabled: bool
    # 自由活动窗口预览的会话数上限 x（每会话取最新一条）
    free_preview_sessions: int
    # 唤醒去重冷却（秒）
    wake_dedup: bool
    wake_cooldown_seconds: float
    # 未读统计窗口（小时）：窗口外未读视为已读（不携带不统计）
    unread_window_hours: float
    # 每会话未读上限：超过上限之前的未读视为已读
    unread_max_per_session: int
    # 唤醒消息引导语（整段前缀）
    wake_notice_prefix: str
    # 消息补充引导语：目标会话在 agent 事件队列里已有事件（上一轮唤醒
    # 通知还没被合并）时，不再重复完整唤醒，只补推新到的（有人找你）消息
    wake_supplement_prefix: str
    no_message_prompt_file: str


def load_beat_config(config: dict) -> BeatConfig:
    """Build BeatConfig from the ``beat`` / ``wake`` YAML sections (all keys
    required; no implicit defaults)."""
    beat = config["beat"]
    wake = config["wake"]
    return BeatConfig(
        enabled=bool(beat["enabled"]),
        interval_seconds=float(beat["interval_seconds"]),
        archive_interval_hours=float(beat["archive_interval_hours"]),
        archive_days=int(beat["archive_days"]),
        initial_probability=float(wake["initial_probability"]),
        min_probability=float(wake["min_probability"]),
        unread_weight=float(wake["unread_weight"]),
        wake_x_convs=int(wake["wake_x_convs"]),
        wake_max_chars=int(wake["wake_max_chars"]),
        inject_latest=bool(wake["inject_latest"]),
        free_preview_enabled=bool(wake["free_preview_enabled"]),
        free_preview_sessions=int(wake["free_preview_sessions"]),
        wake_dedup=bool(wake["wake_dedup"]),
        wake_cooldown_seconds=float(wake["wake_cooldown_seconds"]),
        unread_window_hours=float(config["agent"]["unread_window_hours"]),
        unread_max_per_session=int(
            config["agent"]["unread_max_per_session"]),
        wake_notice_prefix=str(wake["wake_notice_prefix"]),
        wake_supplement_prefix=str(wake["wake_supplement_prefix"]),
        no_message_prompt_file=str(wake["no_message_prompt_file"]),
    )


def _session_of_key(key: str) -> str:
    """Session identity: the first two segments (``platform:conversation_id``)
    of a conversation key. The full three-segment key stays the message-level
    unique identifier."""
    parts = str(key).split(":", 2)
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else str(key)


def _session_of(message: dict[str, Any]) -> str:
    return _session_of_key(message.get("conversation_key", ""))


def _msg_xml(message: dict[str, Any], max_chars: int) -> str:
    """One <msg> element: full message key + identity + time + content.
    sender_data is a child JSON node; the content is llm_content with the
    content fallback. Values are passed through **verbatim** — resident
    never escapes message payloads; any escaping is the upstream skill /
    adapter's own business."""
    content = str(message.get("llm_content") or message.get("content") or "")
    if len(content) > max_chars:
        content = content[:max_chars] + "…"
    sender = message.get("sender_data") or {}
    sender_json = json.dumps(sender, ensure_ascii=False)
    return (f"<msg conversation_key=\"{message['conversation_key']}\""
            f" conversation_type=\"{message.get('conversation_type', '')}\""
            f" msg_time=\"{message.get('created_at', '')}\""
            f" seq=\"{message['seq']}\">"
            f"<sender_data>{sender_json}</sender_data>"
            f"{content}</msg>")


class Beat:
    def __init__(self, agent: Any, messages: MessagesStore,
                 notices: NoticeStore, system_vars: SystemVarsStore,
                 config: BeatConfig,
                 todos: Optional[TodoStore] = None,
                 truncated_dir: str = "") -> None:
        self._agent = agent
        self._messages = messages
        self._notices = notices
        self._system_vars = system_vars
        self._config = config
        self._todos = todos  # Optional[TodoStore]：主动唤醒附带 todo 树
        self._truncated_dir = truncated_dir  # 唤醒截断内容落盘目录
        # 外部组件（事件端口）投递事件的入口；kind 未定义，仅 info
        self._event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop = asyncio.Event()
        self._last_wake_at = 0.0  # 唤醒去重门闩

    # ── event intake (external actors, thread-safe) ────────────────────

    def receive(self, event: dict[str, Any]) -> None:
        """Accept an external event (kind reserved for future extension)."""
        self._event_queue.put(dict(event or {}))

    # ── lifecycle ──────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("beat started interval=%ss", self._config.interval_seconds)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — tick must never die
                logger.error("beat tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.1, self._config.interval_seconds))
            except asyncio.TimeoutError:
                pass

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._task is None:
            self._task = loop.create_task(self.run())

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ── the tick ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        # 1) 唤醒决策（urgent 无条件 / 概率命中）——写 notice
        self._maybe_awake()
        # 2) 定时任务（归档）
        self._run_scheduled()
        # 3) 事件队列 drain（kind 未定义，仅 info）
        self._drain_events()
        # 4) 末尾统一把到期 notice 送入 agent 线程
        self._flush_notices()

    def _run_scheduled(self) -> None:
        last = self._system_vars.get("last_archive_at")
        due = False
        if not last:
            due = True
        else:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                due = (datetime.now() - last_dt) >= timedelta(
                    hours=self._config.archive_interval_hours)
            except ValueError:
                due = True
        if not due:
            return
        moved = self._messages.archive_expired(self._config.archive_days)
        self._system_vars.set("last_archive_at", now_str())
        if moved:
            logger.info("beat archived %d messages", moved)

    def _drain_events(self) -> None:
        drained = 0
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            # kind 是 JSON 对象，后续扩展分派；当前仅记录
            logger.info("beat event drained kind=%s payload=%.200s",
                        json.dumps(event.get("kind", {}), ensure_ascii=False),
                        str(event.get("payload", ""))[:200])
        if drained:
            logger.info("beat drained %d events this tick", drained)

    # ── wake decision ──────────────────────────────────────────────────

    def _current_probability(self) -> float:
        raw = self._system_vars.get("awake_probability")
        try:
            return float(raw) if raw else self._config.initial_probability
        except (TypeError, ValueError):
            return self._config.initial_probability

    def _agent_busy(self) -> bool:
        state = getattr(self._agent, "state", None)
        value = getattr(state, "value", "idle")
        return value != "idle"

    def _queued_conversation_keys(self) -> tuple[set[str], set[str]]:
        """(sessions, full keys) already referenced by events sitting in the
        Agent's event queue (previous wake notices not yet merged; future
        message events may carry keys in metadata)."""
        events = getattr(self._agent, "events", None)
        if events is None:
            return set(), set()
        try:
            snapshot = events.snapshot() \
                if callable(getattr(events, "snapshot", None)) else list(events)
        except Exception:  # noqa: BLE001 — 检测失败按"队列为空"处理
            return set(), set()
        keys: set[str] = set()
        for event in snapshot:
            if isinstance(event, dict):
                metadata = event.get("metadata") or {}
                content = event.get("content", "")
            else:
                metadata = getattr(event, "metadata", None) or {}
                content = getattr(event, "content", "")
            if isinstance(metadata, dict):
                for field in ("conversation_key", "conversation_keys"):
                    value = metadata.get(field)
                    if isinstance(value, str) and value:
                        keys.add(value)
                    elif isinstance(value, (list, tuple, set)):
                        keys.update(str(item) for item in value if item)
            if not isinstance(content, str):
                content = text_of(content)
            keys.update(re.findall(r'conversation_key="([^"]+)"', content or ""))
        return {_session_of_key(key) for key in keys}, keys

    def _maybe_awake(self) -> None:
        """Wake-decision chain:

        1. urgent wake: unread messages marked wakeup=1 exist → fire
           immediately WITHOUT probability check;
        2. wake-dedup cooldown window active → skip the general channel;
        3. agent busy → skip;
        4. otherwise effective probability roll
           (``p * e^(unread_weight * unread)`` capped 1.0) → general wake.
        """
        pending_all = self._messages.pending()
        urgent = [m for m in pending_all if m["wakeup"]]
        if urgent:
            # 紧急消息不受窗口限制（必须及时处理）；会话上限仍适用
            self._fire_wake(pending_all, urgent_messages=urgent)
            return
        # 普通唤醒：窗口外/超上限的未读视为已读（不携带不统计）
        pending = self._messages.pending(
            window_hours=self._config.unread_window_hours,
            max_per_session=self._config.unread_max_per_session)
        if (self._config.wake_dedup
                and self._config.wake_cooldown_seconds > 0):
            if time.time() - self._last_wake_at \
                    < self._config.wake_cooldown_seconds:
                logger.debug("wake skipped: dedup cooldown")
                return
        if self._agent_busy():
            logger.debug("wake skipped: agent busy")
            return
        base = self._current_probability()
        effective = min(1.0, base * math.exp(
            self._config.unread_weight * len(pending)))
        if effective < 1.0 and random.random() >= effective:
            return
        logger.info("proactive awake hit p=%.6f unread=%d effective=%.6f",
                    base, len(pending), effective)
        self._fire_wake(pending, urgent_messages=[])

    def _fire_wake(self, pending: list[dict[str, Any]],
                   urgent_messages: list[dict[str, Any]]) -> None:
        """Assemble the wake payload and write it as a notice (the single
        channel to the Agent). Carried messages are marked read; urgent
        messages are demoted (wakeup=0) after display; the probability is
        cooled to the floor after a wake.

        Quantities whose SESSION already has events in the agent queue take
        the supplement channel instead: no full wake repetition — only the
        new (有人找你) messages, with no earliest-message drag and no
        standard header/footer."""
        queued_sessions, queued_keys = self._queued_conversation_keys()
        content, supplement, carried_keys, demote_keys = \
            self._build_wake_payload(pending, urgent_messages,
                                     queued_sessions, queued_keys)
        todo_block = self._todo_block(urgent_messages)
        now = now_str()
        # 唤醒冷却：概率降到下限；记录唤醒时间（dedup 门闩）
        self._system_vars.set("awake_probability",
                              str(self._config.min_probability))
        self._last_wake_at = time.time()
        if content is None and supplement is None:
            if urgent_messages:
                # 紧急消息都已在队列里：不重复推送；仍降级 wakeup，
                # 防止每 tick 重复紧急唤醒
                for key in demote_keys:
                    try:
                        self._messages.patch(key, {"wakeup": False})
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "demote urgent wakeup failed key=%s", key)
                logger.info("wake skipped: urgent already queued "
                            "pending=%d demoted=%d", len(pending),
                            len(demote_keys))
                return
            # 概率命中但无未读（或未读均已在队列）：无新消息提示词。
            # 真正的自由活动窗口（完全没有 pending）才附带会话最新预览；
            # 未读均已排队时不预览，避免与队列内容重复
            preview = self._free_window_preview() if not pending else ""
            self._notices.create(
                content=(f"当前时间：{now}\n\n"
                         f"{self._load_no_message_prompt()}{todo_block}"
                         f"{preview}"),
                kind="once", due_at=now)
            logger.info("wake fired (no new messages, preview=%s)",
                        bool(preview))
            return
        if content is not None:
            self._notices.create(
                content=(f"当前时间：{now}\n"
                         f"{self._config.wake_notice_prefix}\n\n"
                         f"{content}{todo_block}\n\n"
                         "请注意不要仅凭消息预览就判定群内消息一定与你无关\n"
                         "请自主选择查看未读消息或保持沉默或自主行动"),
                kind="once", due_at=now)
        if supplement is not None:
            # 补充通知：不重复完整开头/结尾；todo 树只在纯补充时附带
            self._notices.create(
                content=(f"{self._config.wake_supplement_prefix}\n\n"
                         f"{supplement}"
                         f"{todo_block if content is None else ''}"),
                kind="once", due_at=now)
        if carried_keys:
            self._messages.mark_sent_to_llm(carried_keys)
        # 紧急消息已注入：降级为普通（wakeup=0），但仍保持未读——只触发
        # 一次紧急唤醒，之后以普通未读存在，避免每 tick 重复紧急唤醒
        demote_only = [key for key in demote_keys
                       if key not in carried_keys]
        for key in demote_only:
            try:
                self._messages.patch(key, {"wakeup": False})
            except Exception:  # noqa: BLE001
                logger.exception("demote urgent wakeup failed key=%s", key)
        logger.info("wake fired kind=%s pending=%d carried=%d demoted=%d "
                    "queued_sessions=%d supplement=%s",
                    "urgent" if urgent_messages else "normal",
                    len(pending), len(carried_keys), len(demote_only),
                    len(queued_sessions), supplement is not None)

    def _todo_block(self, urgent_messages: list[dict[str, Any]]) -> str:
        """Todo tree snippet appended ONLY to proactive wakes (not urgent)."""
        if urgent_messages or self._todos is None:
            return ""
        try:
            if self._todos.is_empty():
                return ""
            return f"\n\n当前 Todo 树：\n{self._todos.markdown()}"
        except Exception:  # noqa: BLE001
            logger.exception("todo tree build failed")
            return ""

    def _free_window_preview(self) -> str:
        """自由活动窗口的消息预览：各会话最新一条（无条件——任何方向/
        已读状态，自己发送的 out 消息也计入），取最新活跃的前 x 个会话。
        只读查询：不改变已读状态（预览不等于消费）。"""
        if not self._config.free_preview_enabled:
            return ""
        limit = int(self._config.free_preview_sessions)
        if limit <= 0:
            return ""
        try:
            rows = self._messages.latest_per_thread(limit)
        except Exception:  # noqa: BLE001 — 预览失败不影响唤醒
            logger.exception("free window preview query failed")
            return ""
        if not rows:
            return ""
        lines = [PREVIEW_HEADER]
        for message in rows:
            lines.append(_msg_xml(message, self._config.wake_max_chars))
        return "\n\n" + "\n".join(lines)

    def _build_wake_payload(
        self, pending: list[dict[str, Any]],
        urgent_messages: list[dict[str, Any]],
        queued_sessions: set[str],
        queued_keys: set[str],
    ) -> tuple[Optional[str], Optional[str], list[str], list[str]]:
        """Group unread messages by session (platform:conversation_id),
        keep the top-x most active sessions, carry the earliest + latest
        message of each (plus ALL urgent messages), DEDUPED by key.
        Sessions with unread but not carried are listed with a notice.

        Sessions already present in the agent event queue (queued_sessions)
        take the SUPPLEMENT channel: only new (有人找你) messages not yet in
        the queue are carried — the earliest message is never re-added there
        (repeated wakes would keep dragging it along).

        Returns (payload, supplement, keys marked read, urgent keys for
        demotion); ``payload``/``supplement`` are None when empty."""
        if not pending:
            return None, None, [], []
        groups: dict[str, list[dict[str, Any]]] = {}
        for message in pending:
            groups.setdefault(_session_of(message), []).append(message)
        ordered: list[dict[str, Any]] = []
        for session, messages in groups.items():
            messages.sort(key=lambda m: m["seq"])
            window = self._config.unread_window_hours
            try:
                count = self._messages.unread_count_by_thread(
                    session, window_hours=window)
            except Exception:  # noqa: BLE001
                count = len(messages)
            ordered.append({
                "session": session,
                "count": count,
                "messages": messages,
                "active_seq": messages[-1]["seq"],
                "has_urgent": any(m["wakeup"] for m in messages),
            })
        # 有紧急消息的会话优先；其余按最新活跃降序
        ordered.sort(key=lambda it: (not it["has_urgent"], -it["active_seq"]))
        carried = ordered[:self._config.wake_x_convs]
        rest = ordered[self._config.wake_x_convs:]

        urgent_by_session: dict[str, list[dict[str, Any]]] = {}
        for message in urgent_messages:
            urgent_by_session.setdefault(
                _session_of(message), []).append(message)

        blocks: list[str] = []
        supplement_blocks: list[str] = []
        carried_keys: list[str] = []
        demote_keys: list[str] = []
        max_chars = self._config.wake_max_chars
        truncated_full: list[str] = []  # 被截断消息的完整内容（不丢弃）
        for it in carried:
            session = it["session"]
            session_urgent = urgent_by_session.get(session, [])
            if session in queued_sessions:
                # 补充通道：该会话已有事件（如上一轮唤醒 notice）在队列里，
                # 不重复完整唤醒；只补推新到的（有人找你）消息，且不携带
                # 最早一条（多轮唤醒一直重复最早消息，浪费上下文）
                demote_keys.extend(
                    m["conversation_key"] for m in session_urgent)
                fresh = [m for m in session_urgent
                         if m["conversation_key"] not in queued_keys]
                if not fresh:
                    continue
                supplement_lines: list[str] = []
                for message in fresh:
                    raw_content = str(message.get("llm_content")
                                      or message.get("content") or "")
                    if len(raw_content) > max_chars:
                        truncated_full.append(f"[{session}] {raw_content}")
                    supplement_lines.append(
                        f"{URGENT_PREFIX}{_msg_xml(message, max_chars)}")
                supplement_blocks.append("\n".join(supplement_lines))
                carried_keys.extend(
                    m["conversation_key"] for m in fresh)
                continue
            session_msgs = it["messages"]
            by_key: dict[str, dict[str, Any]] = {
                m["conversation_key"]: m for m in session_msgs
            }
            earliest_key = session_msgs[0]["conversation_key"]
            # 最新一条：本会话的绝对最新（任何方向——含自己发的 out 消息，
            # 也含已读消息），无条件展示，用作当前对话状态摘要
            latest = session_msgs[-1]
            if self._config.inject_latest:
                try:
                    rows = self._messages.thread_messages_desc(session,
                                                               limit=1)
                    if rows:
                        latest = rows[0]
                except Exception:  # noqa: BLE001 — 查询失败退回待读最新
                    logger.exception("latest message lookup failed "
                                     "session=%s", session)
            latest_key = latest["conversation_key"]
            by_key.setdefault(latest_key, latest)
            single = earliest_key == latest_key

            # 组装待展示的 key 顺序（每 key 只展示一次，保留最早/最晚次序）
            ordered_keys: list[str] = [earliest_key]
            if not single and self._config.inject_latest:
                ordered_keys.append(latest_key)
            urgent_keys: set[str] = set()
            for message in urgent_by_session.get(session, []):
                key = message["conversation_key"]
                urgent_keys.add(key)
                demote_keys.append(key)
                by_key.setdefault(key, message)
                if key not in ordered_keys:
                    ordered_keys.append(key)

            # 展示
            if single:
                lines = [f"{session} 有 {it['count']} 条未读消息，"
                         "其中的一条为："]
            else:
                lines = [f"{session} 有 {it['count']} 条未读消息，"
                         "其中最早的一条为："]
            for index, key in enumerate(ordered_keys):
                message = by_key[key]
                urgent = key in urgent_keys
                raw_content = str(message.get("llm_content")
                                  or message.get("content") or "")
                if len(raw_content) > max_chars:
                    truncated_full.append(f"[{session}] {raw_content}")
                if (not single and index == 1 and key == latest_key
                        and key not in urgent_keys):
                    lines.append("最晚的一条为：")
                    lines.append(_msg_xml(message, max_chars))
                else:
                    prefix = URGENT_PREFIX if urgent else ""
                    lines.append(f"{prefix}{_msg_xml(message, max_chars)}")

            # 展示过的消息一律标记已读（含最新一条）
            carried_keys.extend(ordered_keys)
            # 尾部：已展示 x 条；本会话在该时间窗口内还有多少条未读未展示
            displayed_unread = sum(
                1 for key in ordered_keys
                if by_key[key].get("direction") == "in"
                and not by_key[key].get("sent_to_llm"))
            remaining = max(0, it["count"] - displayed_unread)
            lines.append(TAIL_LINE_TEMPLATE.format(
                displayed=len(ordered_keys), remaining=remaining))
            blocks.append("\n".join(lines))
        for it in rest:
            blocks.append(f"{it['session']} 有 {it['count']} 条未读消息，"
                          f"{NOT_CARRIED_HINT}，请及时处理")
        truncation_hint = ""
        if truncated_full:
            path = self._save_cutoff("\n\n---\n\n".join(truncated_full),
                                     "wake_full")
            if path:
                truncation_hint = (f"被截断消息的完整内容已保存到 {path}，"
                                   "可用 workspace_read 读取")
        normal_text = "\n\n".join(blocks) or None
        supplement_text = "\n\n".join(supplement_blocks) or None
        if truncation_hint and supplement_text is not None:
            supplement_text += f"\n\n{truncation_hint}"
        elif truncation_hint and normal_text is not None:
            normal_text += f"\n\n{truncation_hint}"
        return normal_text, supplement_text, sorted(set(carried_keys)), \
            sorted(set(demote_keys))

    def _save_cutoff(self, text: str, prefix: str) -> Optional[str]:
        """Persist truncated-away wake content into the workspace cache
        dir; returns the runtime path the agent can use (None when
        unavailable). The executor's ``runtime_path`` resolver is preferred
        (sandbox: container realpath; host: bash realpath)."""
        if not text or not self._truncated_dir:
            return None
        workspace = getattr(self._agent, "workspace", None)
        if workspace is None:
            return None
        try:
            directory = Path(self._truncated_dir)
            filename = (f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_"
                        f"{uuid.uuid4().hex[:6]}.txt")
            host = str(directory / filename)
            with open_file_inside(host, workspace.host_root, "w",
                                  create_parents=True,
                                  encoding="utf-8") as handle:
                handle.write(text)
            resolver = getattr(self._agent, "runtime_path", None)
            if callable(resolver):
                try:
                    return resolver(host)
                except Exception:  # noqa: BLE001 — 解析失败退回词法映射
                    pass
            return workspace.to_container(host)
        except Exception:  # noqa: BLE001
            return None

    def _load_no_message_prompt(self) -> str:
        path = Path(self._config.no_message_prompt_file)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("no-message prompt missing: %s", path)
            return f"（主动唤醒，但提示文件缺失: {path}）"

    def _flush_notices(self) -> None:
        """Deliver due notices to the Agent: EVERY due notice becomes its
        own event (x notices -> x events, all pushed this tick). Merging
        several user events into one LLM message is the AGENT framework's
        job (merge_user_events), not the Beat's. The short ``notification``
        text is PREFERRED when present (falls back to the full content) —
        matching the notice_create contract."""
        due = self._notices.due()
        if not due:
            return
        for notice in due:
            body = str(getattr(notice, "notification", "") or "").strip()
            if not body:
                body = str(notice.content)
            self._agent.push_event({
                "role": "user",
                "content": (NOTICE_ENVELOPE.format(
                    notice_id=notice.notice_id, time=now_str())
                    + f"\n{body}"),
                "kind": "notice",
                "metadata": {"notice_id": notice.notice_id},
            })
        logger.info("beat flushed %d notices (%d events)",
                    len(due), len(due))