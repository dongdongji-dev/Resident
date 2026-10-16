"""The Agent: a platform-neutral ReAct loop.

State machine (4 states):

- ``IDLE``      the only state that consumes events. Everything incoming
                stays queued while busy.
- ``STREAMING`` an LLM call is in flight (or the Agent is waiting out the
                batch window right after leaving idle).
- ``TOOL_USE``  tool calls are being executed; their results are pushed
                back onto the queue for the next cycle.
- ``COMPRESS``  context compression is running.

Cycle (per spec):

1. idle: block until the queue is non-empty (do NOT drain).
2. transition to ``STREAMING``, sleep ``event_batch_wait_seconds`` without
   touching the queue (lets a burst land in one batch).
3. check the latest ``repair_latest_n`` context entries; repair / replace
   anything the backend would reject.
4. drain ALL events; tool messages go to the front, user messages merge
   into one (config separator + reminder notice).
5. if (config) the outgoing messages end in ``role=tool``, append a
   temporary empty user message.
6. run ReAct round: stream LLM -> write assistant -> execute tools ->
   enqueue results -> (optional) compress -> back to idle.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import deque
from enum import Enum
from typing import Any, Callable, Optional

from kernel.agent_framework.compression import (CompressionConfig,
                                                compress_context,
                                                load_reminder_prompt,
                                                render_reminder)
from kernel.agent_framework.content import (content_bytes, content_preview,
                                            has_image, strip_images)
from kernel.agent_framework.context import ContextStore
from kernel.agent_framework.events import (Event, EventQueue,
                                           EventTooLargeError)
from kernel.agent_framework.llm.base import (LLMClient, LLMResult,
                                             LLMPermanentError,
                                             RequestTooLargeError, ToolCall)
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.protocol import (append_trailing_empty_user,
                                             is_silence_output,
                                             merge_user_events,
                                             sanitize_tool_messages)
from kernel.agent_framework.reboot import restart_pending
from kernel.osdb.system_vars import SystemVarsStore
from kernel.agent_framework.timeutil import now_plus, now_str
from kernel.agent_framework.tools import ToolRegistry
from kernel.agent_framework.paths import save_cutoff as _save_cutoff
logger = get_logger("kernel.agent_framework.agent")


class AgentState(str, Enum):
    IDLE = "idle"
    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    COMPRESS = "compress"


Observer = Callable[[dict[str, Any]], None]


class Agent:
    """One continuous identity consuming one global event queue."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        context: ContextStore,
        system_prompt: str | Callable[[], str],
        tool_timeout_seconds: float,
        event_batch_wait_seconds: float,
        repair_latest_n: int,
        trailing_empty_user: bool,
        user_merge_enabled: bool,
        user_merge_separator: str,
        user_merge_notice: str,
        system_vars: Optional[SystemVarsStore] = None,
        observer: Optional[Observer] = None,
        compression: Optional[CompressionConfig] = None,
        compression_llm: Optional[LLMClient] = None,
        notices: Optional["NoticeStore"] = None,
        wake_initial_probability: Optional[float] = None,
        wake_min_probability: Optional[float] = None,
        model_output_threshold_chars: Optional[int] = None,
        model_output_line_max_chars: Optional[int] = None,
        truncated_dir: str = "",
        context_max_bytes: int = 0,
        llm_failed_backoff_seconds: float = 0.0,
        tool_result_max_chars: int = 0,
        llm_permanent_fail_threshold: int = 0,
        llm_permanent_fail_backoff_seconds: float = 0.0,
    ) -> None:
        self.llm = llm
        self.tools = registry
        self.context = context
        # 静态字符串或 builder（每次组装消息时从注册表实时渲染工具清单，
        # 注册顺序/后续补充注册都不会让提示词里的工具列表过期）
        self._system_prompt_source: str | Callable[[], str] = system_prompt
        self.tool_timeout_seconds = float(tool_timeout_seconds)
        self.event_batch_wait_seconds = float(event_batch_wait_seconds)
        self.repair_latest_n = max(1, int(repair_latest_n))
        self.trailing_empty_user = bool(trailing_empty_user)
        self.user_merge_enabled = bool(user_merge_enabled)
        self.user_merge_separator = str(user_merge_separator)
        self.user_merge_notice = str(user_merge_notice)
        self.system_vars = system_vars
        self.observer = observer
        self.compression = compression  # Optional[CompressionConfig]
        self.compression_llm = compression_llm  # 独立压缩模型；None 回退主 llm
        self.notices = notices  # Optional[NoticeStore]
        self.llm_failed_backoff_seconds = float(llm_failed_backoff_seconds)
        # 永久性 LLM 故障隔离：连续 threshold 次 LLMPermanentError（0=关闭）
        # 后进入低频探测模式（只打一条 ERROR、事件保留在队列），成功即恢复
        self.llm_permanent_fail_threshold = max(
            0, int(llm_permanent_fail_threshold or 0))
        self.llm_permanent_fail_backoff_seconds = max(
            0.0, float(llm_permanent_fail_backoff_seconds or 0.0))
        self._llm_fail_streak = 0
        self._llm_permanent_fail_streak = 0
        self._llm_isolated = False
        self.wake_initial_probability = (
            float(wake_initial_probability)
            if wake_initial_probability is not None else None)
        self.wake_min_probability = (
            float(wake_min_probability)
            if wake_min_probability is not None else None)
        self.model_output_threshold_chars = (
            int(model_output_threshold_chars)
            if model_output_threshold_chars is not None else None)
        self.model_output_line_max_chars = (
            int(model_output_line_max_chars)
            if model_output_line_max_chars is not None else None)
        self.truncated_dir = truncated_dir
        self.context_max_bytes = max(0, int(context_max_bytes or 0))
        # 工具结果通用上限（0=不限制）：工具自身漏掉截断时的兜底
        self.tool_result_max_chars = max(0, int(tool_result_max_chars or 0))
        # 统一"内核产物 -> agent 可用路径"解析器（executor.runtime_path；
        # 沙箱=容器内 realpath，宿主机=bash realpath）。None 时退回词法映射。
        self.runtime_path: Optional[Callable[[str], str]] = None

        self.events = EventQueue()
        self._state = AgentState.IDLE
        self._state_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._round = 0

    # ── observable state ────────────────────────────────────────────────
    @property
    def system_prompt(self) -> str:
        """The full system prompt. When constructed with a builder callable
        (``make_system_prompt_builder``), it re-renders from the LIVE tool
        registry on every access — the registry is the single source of
        truth, so the tools section is always complete."""
        source = self._system_prompt_source
        return source() if callable(source) else str(source)

    @property
    def state(self) -> AgentState:
        with self._state_lock:
            return self._state

    @property
    def round_number(self) -> int:
        return self._round

    def _set_state(self, state: AgentState) -> None:
        with self._state_lock:
            self._state = state
        logger.debug("state changed: %s", state.value)
        self._publish({"type": "state_changed", "state": state.value})

    def _publish(self, event: dict[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            self.observer(event)
        except Exception:  # noqa: BLE001 — telemetry must never kill loop
            pass

    # ── event intake (any thread) ───────────────────────────────────────
    def pending_bytes(self) -> int:
        """context.jsonl bytes + queued (not yet consumed) event bytes."""
        total = self.context.size_bytes()
        try:
            queued = self.events.snapshot()
        except Exception:  # noqa: BLE001 — budget check must never block
            queued = []
        for item in queued:
            total += content_bytes(item.content)
        return total

    def _check_event_bytes(self, event: Event) -> None:
        """Image-carrying events must fit the context byte budget before
        entering the queue (text events keep the token-compression path so
        notices/reminders are never refused). Raises EventTooLargeError."""
        if self.context_max_bytes <= 0 or not has_image(event.content):
            return
        total = self.pending_bytes() + content_bytes(event.content)
        if total > self.context_max_bytes:
            raise EventTooLargeError(
                f"拒绝添加图片事件：context+队列+本事件约 {total} 字节，"
                f"超过 agent.context_max_bytes={self.context_max_bytes}；"
                "请等待上下文压缩或清理后再试")

    def push_event(self, value: Event | dict[str, Any]) -> str:
        event = Event.from_value(value)
        self._check_event_bytes(event)
        self.events.push(event)
        logger.info("event queued event_id=%s role=%s kind=%s content=%.120s",
                    event.event_id, event.role, event.kind,
                    content_preview(event.content))
        self._publish({"type": "event_queued",
                       "event": event.as_context_message()})
        return event.event_id

    def stop(self) -> None:
        self._stop.set()
        self.events.close()

    # ── the ReAct loop ──────────────────────────────────────────────────
    async def run(self) -> None:
        self.events.attach_loop(asyncio.get_running_loop())
        logger.info("agent run started")
        self._set_state(AgentState.IDLE)
        try:
            while not self._stop.is_set():
                if restart_pending():
                    # 重启信号：干净退出 run 循环，由 main() 负责重启
                    logger.info("reboot requested; exiting run loop")
                    break
                # 1) idle wait: block until non-empty, do NOT drain yet
                await self.events.wait_nonempty()
                # 2) streaming begins; leave events queued, sleep to batch
                self._set_state(AgentState.STREAMING)
                if self.event_batch_wait_seconds > 0:
                    await asyncio.sleep(self.event_batch_wait_seconds)
                # 3) repair the context tail before the backend sees it
                repaired, dropped = self.context.repair_latest(
                    self.repair_latest_n)
                if repaired or dropped:
                    logger.warning("round tail repair: repaired=%d dropped=%d",
                                   repaired, dropped)
                # 4) drain everything
                events = self.events.drain()
                if not events:
                    self._set_state(AgentState.IDLE)
                    continue
                prepared = self._prepare_events(events)
                # 本轮事件立即写入 context 并发布（用户消息即时显示、保持
                # 顺序）；失败重试轮按 event_id 去重，避免重复写入。
                for event in prepared:
                    message = event.as_context_message()
                    eid = message.get("event_id")
                    if eid and self.context.has_event_id(eid):
                        continue
                    self.context.append(message)
                    self._publish({"type": "context_appended",
                                   "message": message})
                if not await self._run_round(prepared):
                    # LLM 调用失败（重试耗尽）：重新入队，退避后由下一轮
                    # 自动重试——消息不会被卡死。context 中已写入的本轮事件
                    # 保留（按 event_id 去重，重试轮不会重复写入）。
                    for event in prepared:
                        self.events.push(event)
                    # 隔离态用低频探测间隔（不再按瞬态退避快刷）
                    delay = self.llm_permanent_fail_backoff_seconds \
                        if self._llm_isolated \
                        else self.llm_failed_backoff_seconds
                    if delay > 0:
                        await asyncio.sleep(delay)
        finally:
            logger.info("agent run stopped")
            self._set_state(AgentState.IDLE)

    def _prepare_events(self, events: list[Event]) -> list[Event]:
        """tool messages first, then non-tool in relative order, then user
        messages merged (per config) into one."""
        tools = [e for e in events if e.role == "tool"]
        others = [e for e in events if e.role != "tool"]
        prepared = tools + others
        return merge_user_events(
            prepared, self.user_merge_enabled,
            self.user_merge_separator, self.user_merge_notice)

    async def _run_round(self, prepared: Optional[list[Event]] = None) -> bool:
        """Run one ReAct round. ``prepared`` (optional) are the drained events
        of this round; they are written to context by ``run()`` BEFORE the LLM
        call (so user messages show up immediately and ordering is preserved).
        A failed round is re-enqueued by the loop; events already in context
        are filtered here so the retry does not feed them to the LLM twice.
        Returns False on LLM failure."""
        self._round += 1
        round_id = self._round
        self._set_state(AgentState.STREAMING)

        # 预压缩（修复必需）：上下文已超阈值时先压缩再发 LLM——否则把
        # 超长上下文（如重启后遗留的 1M tokens）发给模型必然 400，且
        # 轮失败不会走到轮末压缩，形成死循环。
        if self.compression is not None:
            await self._maybe_compress()
        # 预压缩若触发过压缩会把状态切到 IDLE，这里恢复 STREAMING
        # （LLM 调用期间前端应显示 streaming）
        self._set_state(AgentState.STREAMING)

        # 本轮事件已由 run() 提前写入 context（即时显示）。这里仅构建 LLM
        # 输入：过滤已在 context 的事件（失败重试轮去重），避免重复。
        messages = self._build_messages(prepared)

        self._publish({"type": "round_started", "round_id": round_id})
        logger.info("round=%d start messages=%d tools=%d", round_id,
                    len(messages), len(self.tools.schemas()))

        def on_delta(channel: str, text: str) -> None:
            # telemetry only; adapters call it synchronously per stream chunk
            self._publish({"type": "token", "round_id": round_id,
                           "channel": channel, "delta": text})

        async def stream_once(payload: list[dict[str, Any]]) -> LLMResult:
            return await self.llm.stream(
                payload, tools=self.tools.schemas(), on_delta=on_delta)

        try:
            result = await stream_once(messages)
        except RequestTooLargeError as exc:
            # 请求被 413/too-large 拒绝：仅当本次请求确实携带图片块（LLM
            # 声明了 image 输入）才做降级；纯文本配置下图片本来就没在请求里，
            # 不能在 text 超限时误删 context 中的图片。降级 = 把 context 里
            # 的图片块永久替换为占位符，重建请求重试一次。
            if not self._request_carries_images(messages):
                return self._fail_round(round_id, exc)
            dropped = self._drop_images_from_context()
            if not dropped:
                return self._fail_round(round_id, exc)
            self._publish({"type": "images_dropped", "round_id": round_id,
                           "count": dropped})
            logger.warning(
                "round=%d request too large; dropped image blocks from %d "
                "context entries and retrying once", round_id, dropped)
            try:
                result = await stream_once(
                    self._build_messages(prepared, drop_images=True))
            except Exception as retry_exc:  # noqa: BLE001
                return self._fail_round(round_id, retry_exc)
        except Exception as exc:  # noqa: BLE001
            return self._fail_round(round_id, exc)

        logger.info(
            "round=%d done tool_calls=%d usage=%s\n  [reasoning] %s\n  [output] %s",
            round_id, len(result.tool_calls),
            json.dumps(result.usage or {}, ensure_ascii=False),
            (result.thinking or "")[:800].replace("\n", "\n  "),
            (result.text or "")[:800].replace("\n", "\n  "))

        # 唤醒概率维护 + 自说自话检测（agent 线程）
        self._maintain_wake_probability(bool(result.tool_calls))
        if not result.tool_calls:
            self._record_model_output(result.text)

        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": result.text,
        }
        # 记录产出该条目的模型名（微调数据归属：区分哪条数据哪个模型生成）
        model_name = str(getattr(self.llm, "model", "") or "").strip()
        if model_name:
            assistant["model"] = model_name
        if result.usage:
            assistant["usage"] = result.usage
        if result.thinking:
            assistant["reasoning_content"] = result.thinking
        if result.provider_items:
            assistant["provider_items"] = result.provider_items
        if result.tool_calls:
            assistant["tool_calls"] = [
                {"id": c.call_id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in result.tool_calls
            ]
        # 本轮事件已由 run() 提前写入 context（即时显示）；这里仅写入
        # assistant（LLM 成功结果）。
        self.context.append(assistant)
        self._publish({"type": "context_appended", "message": assistant,
                       "round_id": round_id})

        if result.tool_calls:
            await self._run_tools(result.tool_calls, round_id)

        self._publish({"type": "round_finished", "round_id": round_id,
                       "has_tool_calls": bool(result.tool_calls)})
        self._reset_llm_failure_state()  # LLM 恢复（若此前处于隔离态）
        await self._maybe_compress()
        self._set_state(AgentState.IDLE)
        return True

    def _build_messages(self, prepared: Optional[list[Event]] = None,
                        drop_images: bool = False) -> list[dict[str, Any]]:
        """Build the outgoing message list from context + this round's events
        (deduped by event_id for failed-round retries). With ``drop_images``
        image blocks flatten to placeholders as a last line of defense."""
        # 去重：context 里已有的 event_id，外加检查点 metadata 声明的
        # covered_event_ids（展平进检查点的当前批次——它们的内容已在
        # checkpoint 文本里，不能再作为 incoming 重复附加）
        known_ids: set[str] = set()
        for entry in self.context.load():
            event_id = entry.get("event_id")
            if event_id:
                known_ids.add(str(event_id))
            metadata = entry.get("metadata") or {}
            for covered in (metadata.get("covered_event_ids") or []):
                known_ids.add(str(covered))
        incoming = [event.as_context_message() for event in (prepared or [])
                    if event.event_id not in known_ids]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}]
        messages.extend(self.context.messages_for_llm())
        messages.extend(incoming)
        if drop_images:
            for message in messages:
                if has_image(message.get("content")):
                    message["content"] = strip_images(message["content"])
        messages = sanitize_tool_messages(messages)
        messages = append_trailing_empty_user(
            messages, self.trailing_empty_user)
        return messages

    def _request_carries_images(self, messages: list[dict[str, Any]]) -> bool:
        """True when the outgoing request can actually include image blocks.
        Unknown LLM clients (no declared modalities) are treated as capable —
        dropping images is then the conservative way out of a 413 loop."""
        modalities = getattr(self.llm, "input_modalities", None)
        if modalities is not None and "image" not in tuple(modalities):
            return False
        return any(has_image(message.get("content")) for message in messages)

    def _bound_tool_result(self, name: str, content: str) -> str:
        """工具结果通用上限：任何工具输出超过 ``tool_result_max_chars``
        即截断，完整结果落盘并附读取路径（防个别工具漏设截断，把单条
        base64/全量查询结果整块塞进上下文）。"""
        limit = self.tool_result_max_chars
        if limit <= 0 or len(content) <= limit:
            return content
        path = ""
        if self.truncated_dir and getattr(self, "workspace", None) is not None:
            try:
                path = _save_cutoff(self.workspace, content,
                                    f"tool_{name}", self.truncated_dir,
                                    expose=self.runtime_path) or ""
            except Exception:  # noqa: BLE001 — 落盘失败退化为纯截断
                path = ""
        hint = (f"（工具结果过长：共 {len(content)} 字符，已截断至 {limit}"
                + (f"；完整结果已保存到 {path}，可用 workspace_read 读取）"
                   if path else "）"))
        return content[:limit] + "\n…" + hint

    def _fail_round(self, round_id: int, exc: BaseException) -> bool:
        """Round failure: classify PERMANENT provider rejections (bad key /
        unknown model / no permission — non-retryable 4xx) apart from
        transient ones. After ``llm_permanent_fail_threshold`` consecutive
        permanent failures the agent enters an isolated low-frequency probe
        mode: ONE error log + one ``llm_unavailable`` event (no per-round
        spam), queued events are kept, and a successful request resets
        everything (auto-recovery)."""
        permanent = isinstance(exc, LLMPermanentError)
        self._llm_fail_streak += 1
        if permanent:
            self._llm_permanent_fail_streak += 1
        else:
            self._llm_permanent_fail_streak = 0
        threshold = self.llm_permanent_fail_threshold
        if (permanent and threshold > 0
                and self._llm_permanent_fail_streak >= threshold):
            if not self._llm_isolated:
                self._llm_isolated = True
                logger.error(
                    "LLM permanent failure isolated: %d consecutive "
                    "failures (%s); probing every %gs until the "
                    "configuration is fixed (queued events are kept)",
                    self._llm_permanent_fail_streak, exc,
                    self.llm_permanent_fail_backoff_seconds)
                self._publish({
                    "type": "llm_unavailable", "round_id": round_id,
                    "error": str(exc),
                    "fail_streak": self._llm_permanent_fail_streak,
                    "retry_in": self.llm_permanent_fail_backoff_seconds,
                })
            else:
                logger.info("LLM still unavailable (probe failed): %s", exc)
        else:
            logger.warning("round=%d FAILED: %s", round_id, exc)
            self._publish({"type": "round_failed", "round_id": round_id,
                           "error": str(exc)})
        self._set_state(AgentState.IDLE)
        return False

    def _reset_llm_failure_state(self) -> None:
        """One successful request means the LLM is back: clear failure
        counters, leave isolation, announce recovery once."""
        if self._llm_isolated:
            logger.info("LLM recovered after %d permanent failures; "
                        "resuming normal processing",
                        self._llm_permanent_fail_streak)
            self._publish({"type": "llm_recovered",
                           "recovered_streak":
                               self._llm_permanent_fail_streak})
        self._llm_fail_streak = 0
        self._llm_permanent_fail_streak = 0
        self._llm_isolated = False

    def _drop_images_from_context(self) -> int:
        """Permanently replace every image block in context.jsonl with a
        placeholder; returns the number of changed entries."""
        entries = self.context.load()
        changed = 0
        for entry in entries:
            if has_image(entry.get("content")):
                entry["content"] = strip_images(entry["content"])
                changed += 1
        if changed:
            self.context.write_all(entries)
        return changed

    async def _run_tools(self, calls: list[ToolCall], round_id: int) -> None:
        self._set_state(AgentState.TOOL_USE)

        async def one(call: ToolCall) -> tuple[ToolCall, str]:
            logger.info("round=%d tool_start name=%s call_id=%s params=%s",
                        round_id, call.name, call.call_id,
                        json.dumps(call.params, ensure_ascii=False)[:200])
            self._publish({"type": "tool_started", "round_id": round_id,
                           "tool_call_id": call.call_id, "name": call.name,
                           "params": call.params})
            started = time.monotonic()
            try:
                raw = await asyncio.wait_for(
                    self.tools.invoke(call.name, call.params),
                    timeout=self.tool_timeout_seconds)
                content = str(raw)
            except asyncio.TimeoutError:
                content = (f"工具 {call.name} 超时"
                           f"（>{self.tool_timeout_seconds:g}s）")
            except Exception as exc:  # noqa: BLE001
                content = f"工具 {call.name} 失败：{exc}"
            content = self._bound_tool_result(call.name, content)
            logger.info("round=%d tool_done name=%s elapsed=%.3fs",
                        round_id, call.name, time.monotonic() - started)
            self._publish({
                "type": "tool_finished", "round_id": round_id,
                "tool_call_id": call.call_id, "name": call.name,
                "elapsed": round(time.monotonic() - started, 3),
                "content": content,
            })
            return call, content

        results = await asyncio.gather(*(one(c) for c in calls))
        for call, content in results:
            if self._stop.is_set():
                break
            self.push_event(Event(
                role="tool",
                content=content,
                kind="tool_result",
                source="runtime",
                metadata={"tool_call_id": call.call_id, "name": call.name},
            ))

    # ── wake probability + model-output self-talk (agent thread) ──────

    def pause_free_text(self, seconds: float) -> str:
        """Pause the self-talk (free-text) detection for ``seconds``; the
        deadline is persisted to system_vars (survives restarts) and the
        detection auto-resumes when it passes."""
        seconds = max(0.0, float(seconds))
        until = now_plus(int(seconds))
        if self.system_vars is not None:
            self.system_vars.set("free_text_paused_until", until)
        return (f"游离文本检测已暂停 {seconds:g} 秒，"
                f"到期自动恢复（{until}）")

    def _free_text_active(self) -> bool:
        """True when the self-talk detection is NOT paused: no deadline set
        or now is past the deadline."""
        if self.system_vars is None:
            return True
        until = self.system_vars.get("free_text_paused_until")
        if not until:
            return True
        return now_str() > until

    def _maintain_wake_probability(self, has_tools: bool) -> None:
        """Tool call this round -> reset to initial; no tool call -> halve
        (floor at min). Persisted so the Beat can read it."""
        if (self.system_vars is None
                or self.wake_initial_probability is None
                or self.wake_min_probability is None):
            return
        raw = self.system_vars.get("awake_probability")
        try:
            probability = float(raw) if raw else self.wake_initial_probability
        except (TypeError, ValueError):
            probability = self.wake_initial_probability
        new_probability = self.wake_initial_probability if has_tools \
            else max(self.wake_min_probability, probability / 2)
        self.system_vars.set("awake_probability", str(new_probability))

    def _self_talk_reason(self, text: str) -> str:
        """Why this non-tool output should be flagged ('' = nothing):
        over the single-message threshold, multiple consecutive newlines, or
        multiple markdown code fences."""
        if len(text) >= self.model_output_threshold_chars:
            return "本次输出超过字数阈值"
        if re.search(r"\n\s*\n\s*\n", text):
            return "输出包含多个连续换行"
        if text.count("```") >= 2:
            return "输出包含多个 markdown 代码块"
        return ""

    def _record_model_output(self, text: str) -> None:
        """Persist model text produced WITHOUT a tool call (self-talk), line
        separated. Flagged (notice + buffer clear) when: the accumulated
        chars exceed the threshold, OR the current output has multiple
        consecutive newlines, OR it exceeds the threshold on its own, OR it
        carries multiple markdown code fences. Silent / paused outputs are
        ignored."""
        if (self.system_vars is None or self.notices is None
                or self.model_output_threshold_chars is None):
            return
        text = str(text or "")
        if not text or is_silence_output(text):
            # 空输出 / 静默标记：不计入自说自话字数
            return
        if not self._free_text_active():
            # 屏蔽期间：不通知也不记录
            return
        cached = self.system_vars.get("model_output_cache") or ""
        cached = cached.rstrip("\n") + "\n" + text.lstrip("\n") \
            if cached else text
        reason = self._self_talk_reason(text)
        accumulated = len(cached) >= self.model_output_threshold_chars
        if not reason and not accumulated:
            self.system_vars.set("model_output_cache", cached)
            return
        max_chars = self.model_output_line_max_chars or 100
        lines = [line for line in cached.splitlines() if line.strip()]
        snippet = "\n".join(
            f"[{i}]{line[:max_chars]}" for i, line in enumerate(lines, 1))
        content = (
            "系统检测到你在最近输出了这些内容：\n"
            f"{snippet}\n"
            "注意这些内容你没有向任何人发送，请确认是否是你的正常预期行为，"
            "如果想要发送消息，请使用对应软件的发送消息方法")
        if reason:
            content = (f"检测到疑似游离文本（{reason}），"
                       f"请确认是否有对外动作遗漏。\n\n{content}")
        # 完整缓存落盘（不丢弃），通知里附运行时路径
        if self.truncated_dir and getattr(self, "workspace", None) is not None:
            path = _save_cutoff(self.workspace, cached, "model_output",
                                self.truncated_dir, expose=self.runtime_path)
            if path:
                content += (f"\n完整内容已保存到 {path}，"
                            "可用 workspace_read 读取")
        try:
            self.notices.create(content=content, kind="once",
                                due_at=now_str())
        except Exception as exc:  # noqa: BLE001 — 检测失败不影响主循环
            logger.warning("model_output notice create failed: %s", exc)
            return
        self.system_vars.set("model_output_cache", "")
        logger.info("model_output self-talk notice created "
                    "(chars=%d reason=%r)", len(cached), reason)

    def _consume_compress_request(self) -> bool:
        """Read-and-clear the ``compress_requested`` flag (os.db system_vars).
        Other subsystems (e.g. readimage hitting the byte budget) set it to
        'true' to request compression at the next safe point."""
        if self.system_vars is None:
            return False
        raw = self.system_vars.get("compress_requested")
        if str(raw or "").strip().lower() not in ("true", "1", "yes"):
            return False
        self.system_vars.set("compress_requested", "false")
        logger.info("compression force-requested (system_vars)")
        return True

    async def _maybe_compress(self) -> None:
        """Two-step compression (remember extraction + summary/keep-recent)
        once the accumulated tokens exceed ``max_tokens * threshold`` — or
        immediately when ``compress_requested`` is set in system_vars.
        On success a notice is created so the Agent learns the context was
        compacted (delivered by the Beat like every other notice).
        NOT triggered -> returns WITHOUT touching the state (callers may
        invoke this mid-round as a pre-check; only an actual compression
        transitions COMPRESS -> IDLE)."""
        if self.compression is None:
            return
        if self._llm_isolated:
            # LLM 永久性故障隔离中：压缩走同一个 LLM，只会徒劳重试
            return
        # 队列还有未消费事件（工具结果等）时压缩会让它们变孤儿：checkpoint
        # 重写后 tool 结果配不上 assistant tool_calls，会被 sanitize 丢弃。
        # 延后到下一轮轮首安全点（run() 先把事件写入 context 再预压缩）。
        if len(self.events) > 0:
            return
        force = self._consume_compress_request()
        # 估算 = 最新 provider usage + 其后追加条目的内容估算（usage 落后
        # 于尾部工具结果，单靠它会让超长上下文永远触发不了压缩）
        total = self.context.estimated_tokens(
            self.compression.estimate_chars_per_token,
            self.compression.image_tokens_estimate)
        if not force and total <= self.compression.max_tokens \
                * self.compression.threshold:
            # 未触发：不动状态（预压缩调用点在轮中，状态由调用方维持）
            return
        self._set_state(AgentState.COMPRESS)
        try:
            summary = await compress_context(
                self.context, self.compression_llm or self.llm,
                self.compression, force=force)
            if summary is not None:
                self._publish({"type": "context_compressed",
                               "summary": summary})
                # 压缩完成：把整理提醒作为 user 消息【立即入队触发下一轮】，
                # 而不是等 beat 定时 flush——否则压缩完直接 idle 发呆。
                # 提醒模板来自 compression.reminder_file（{memory_file} 占位符）
                try:
                    memory_file = ""
                    if (self.compression.memory
                            and self.compression.memory.enabled):
                        memory_file = self.compression.memory.file
                        resolved = ""
                        if self.runtime_path is not None:
                            try:
                                resolved = self.runtime_path(memory_file)
                            except Exception:  # noqa: BLE001
                                resolved = ""
                        if not resolved:
                            workspace = getattr(self, "workspace", None)
                            if workspace is not None:
                                try:
                                    resolved = workspace.to_container(
                                        memory_file)
                                except Exception:  # noqa: BLE001
                                    resolved = ""
                        if resolved:
                            memory_file = resolved
                    reminder = render_reminder(
                        load_reminder_prompt(self.compression.reminder_file),
                        memory_file)
                    self.push_event({
                        "role": "user",
                        "content": reminder,
                        "kind": "notice",
                        "source": "runtime",
                    })
                    logger.info("compression reminder pushed to agent")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("compression reminder push failed: %s",
                                   exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("compression failed: %s", exc)
            self._publish({"type": "compression_failed", "error": str(exc)})
        finally:
            self._set_state(AgentState.IDLE)
