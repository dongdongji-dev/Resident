"""The Agent: a platform-neutral ReAct loop（subagent 从0重建版）.

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

工具结果带状态码（设计约定）：handler 返回 dict 含 ``code`` 即按
该字段判定，否则视为成功（200）。非200（用户拒绝/取消/超时/失败）时结果
**正常写入 context（文件+内存），但不 push 回事件队列**——不触发下一轮自动
call，agent 回 IDLE 等外部新消息，避免模型把"被拒"当成功结果继续自主推进。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from kernel.agent_framework.context import ContextStore
from kernel.agent_framework.events import Event, EventQueue
from kernel.agent_framework.llm.base import LLMClient, ToolCall
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.protocol import (append_trailing_empty_user,
                                             merge_user_events,
                                             sanitize_tool_messages)
from kernel.agent_framework.tools import ToolRegistry

logger = get_logger("kernel.agent_framework.agent")

# ── tool result 状态码层（设计约定）─────────────────────────────
TOOL_STATUS_OK = 200
TOOL_STATUS_REJECTED = 403   # 拒绝执行
TOOL_STATUS_TIMEOUT = 408
TOOL_STATUS_FAILED = 500
TOOL_STATUS_CANCELLED = 499  # cancel

# 中断类（本次事务写 context 不 call）：拒绝执行 / cancel
INTERRUPT_CODES = {TOOL_STATUS_REJECTED, TOOL_STATUS_CANCELLED}


def _read_tool_status(raw: Any) -> tuple[int, str]:
    """读取 ``(status_code, content)``。

    - dict 带 ``code`` key → 读该 code（可转 int），content 缺省为剩余载荷
      （去掉 code 后）。
    - 其它 → 视为成功 (200)，content = str(raw)。
    """
    if isinstance(raw, dict) and raw.get("code") is not None:
        try:
            code = int(raw["code"])
        except (TypeError, ValueError):
            code = TOOL_STATUS_OK
        content = raw.get("content")
        if content is None:
            content = json.dumps(
                {k: v for k, v in raw.items() if k != "code"},
                ensure_ascii=False)
        return code, str(content)
    return TOOL_STATUS_OK, str(raw)


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
        system_prompt: str,
        tool_timeout_seconds: float,
        event_batch_wait_seconds: float,
        repair_latest_n: int,
        trailing_empty_user: bool,
        user_merge_enabled: bool,
        user_merge_separator: str,
        user_merge_notice: str,
        observer: Optional[Observer] = None,
        name: Optional[str] = None,
        compression: Optional[Any] = None,
        compression_llm: Optional[Any] = None,
        tool_logger: Optional[Callable[[dict[str, Any]], None]] = None,
        failure_cfg: Optional[dict[str, Any]] = None,
    ) -> None:
        self.llm = llm
        self.tools = registry
        self.context = context
        self.system_prompt = system_prompt
        self.name = name or "subagent"
        self.tool_timeout_seconds = float(tool_timeout_seconds)
        self.event_batch_wait_seconds = float(event_batch_wait_seconds)
        self.repair_latest_n = max(1, int(repair_latest_n))
        # 失败退避（2026-10-13）：LLM 调用失败不是立刻重试，而是按指数退避，
        # 连续失败到 failure_alert_after 次发一次 round_failed notice，
        # 到 failure_cooldown_after 次转入长效冷却（默认 300s）——
        # 防止后端 400/订阅过期这类持续性错误把 agent 打成死循环烧额度。
        _fcfg = dict(failure_cfg or {})
        self.fail_backoff_base_s = float(_fcfg.get("backoff_base_s", 0.5))
        self.fail_backoff_max_s = float(_fcfg.get("backoff_max_s", 60))
        self.fail_alert_after = int(_fcfg.get("alert_after", 3))
        self.fail_cooldown_after = int(_fcfg.get("cooldown_after", 5))
        self.fail_cooldown_s = float(_fcfg.get("cooldown_s", 300))
        self._fail_streak = 0
        self._fail_alerted = False
        self.trailing_empty_user = bool(trailing_empty_user)
        self.user_merge_enabled = bool(user_merge_enabled)
        self.user_merge_separator = str(user_merge_separator)
        self.user_merge_notice = str(user_merge_notice)
        self.observer = observer
        # 上下文压缩：compression=CompressionConfig(None=禁用)；
        # compression_llm=独立压缩模型 client（缺省复用主 llm）。
        self.compression = compression
        self.compression_llm = compression_llm
        self._compress_lock = threading.Lock()
        # 工具调用审计日志钩子（每次 invoke 前调用，记录"用了什么工具/什么参数/
        # 调用时间"）；None 则不记录。收一个 dict：{time, tool, params, call_id}。
        self.tool_logger = tool_logger

        self.events = EventQueue()
        self._state = AgentState.IDLE
        self._state_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._round = 0

    # ── observable state ────────────────────────────────────────────────
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
    def push_event(self, value: Event | dict[str, Any]) -> str:
        event = self.events.push(value)
        logger.info("event queued event_id=%s role=%s kind=%s content=%.120s",
                    event.event_id, event.role, event.kind,
                    (event.content or "").replace("\n", " "))
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
                # 本轮事件立即写入 context（保持顺序）；失败重试轮按
                # event_id 去重，避免重复写入。
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
                    # 自动重试。context 已写入的本轮事件保留（按 event_id
                    # 去重，重试轮不会重复写入）。
                    for event in prepared:
                        self.events.push(event)
                    await self._sleep_after_failure()
                    continue
                if self._fail_streak:
                    logger.info("LLM 恢复正常（此前连续失败 %d 次）",
                                self._fail_streak)
                self._fail_streak = 0
                self._fail_alerted = False
                # 一轮完成，回 IDLE 前：达到窗口阈值则自动压缩（COMPRESS 态）。
                await self._maybe_compress()
        finally:
            logger.info("agent run stopped")
            self._set_state(AgentState.IDLE)

    async def _sleep_after_failure(self) -> None:
        """LLM 调用失败后的退避等待（2026-10-13 加）。

        之前固定 sleep(0.2) → 后端持续 400（如订阅过期）时会把 agent 打成
        每秒几十轮的死循环：既烧日志也烧额度，而且用户完全看不出出了什么事。

        现在：指数退避 0.5 → 1 → 2 … 封顶 60s；连续失败达 alert_after 次
        发一次 round_failed notice（只发一次，恢复后重置）；达 cooldown_after
        次转长效冷却 300s（仍是等待，不丢事件——事件已重新入队）。
        """
        self._fail_streak += 1
        if self._fail_streak >= self.fail_cooldown_after:
            delay = self.fail_cooldown_s
        else:
            delay = min(self.fail_backoff_base_s * (2 ** (self._fail_streak - 1)),
                        self.fail_backoff_max_s)
        if (self._fail_streak >= self.fail_alert_after
                and not self._fail_alerted):
            self._fail_alerted = True
            self._publish({
                "type": "notice", "kind": "round_failed", "agent": self.name,
                "message": (f"实例 {self.name} 连续 {self._fail_streak} 轮 "
                            f"LLM 调用失败，已转入退避（{delay:.0f}s/次）。"
                            f"多半是后端不可用/额度问题，请看 hub 日志。"),
            })
        logger.warning("第 %d 次连续失败，退避 %.1fs", self._fail_streak, delay)
        await asyncio.sleep(delay)

    def _prepare_events(self, events: list[Event]) -> list[Event]:
        """tool messages first, then non-tool in relative order, then user
        messages merged (per config) into one."""
        tools = [e for e in events if e.role == "tool"]
        others = [e for e in events if e.role != "tool"]
        prepared = tools + others
        return merge_user_events(
            prepared, self.user_merge_enabled,
            self.user_merge_separator, self.user_merge_notice)

    # ── context compression ─────────────────────────────────────────────
    async def _maybe_compress(self) -> bool:
        """一轮完成后自动判断：total > max_tokens*threshold 则压缩。返回是否压缩。
        compression 未配置（None）时静默跳过。"""
        if self.compression is None:
            return False
        try:
            from kernel.agent_framework import compression as _c
            if not await _c.should_compress(self.context, self.compression):
                return False
        except Exception as exc:  # noqa: BLE001 — 压缩失败不影响 agent 循环
            logger.warning("compression check failed: %s", exc)
            return False
        summary = await self._run_compress()
        return summary is not None

    async def _run_compress(self, force: bool = False) -> Optional[str]:
        """执行一次上下文压缩（手动/自动共用）。``force=True`` 手动触发时
        忽略阈值强制压缩。返回摘要文本，失败返回 None（fallback checkpoint
        会写好，上下文一定降下来）。"""
        if self.compression is None:
            return None
        from kernel.agent_framework import compression as _c
        self._set_state(AgentState.COMPRESS)
        llm = self.compression_llm or self.llm
        try:
            summary = await _c.compress_context(
                self.context, llm, self.compression, force=force)
            if summary:
                self._publish({"type": "notice", "kind": "compressed",
                               "agent": self.name,
                               "message": f"实例 {self.name} 上下文已压缩。",
                               "model_output": summary})
            return summary
        finally:
            self._set_state(AgentState.IDLE)

    def _accumulate_usage(self, usage: dict) -> None:
        """把单轮 usage 的三类 token 累计进 session 元数据（session.json 的
        usage_total 段），供额度统计。三类（设计约定）：
        cached_input = prompt_tokens_details.cached_tokens（每轮累积，呈二次方）
        uncached_input = prompt_tokens - cached_tokens
        output = completion_tokens
        session.json 与 context.jsonl 同目录（session 领地），每轮累加后覆写。"""
        if not usage or not isinstance(usage, dict):
            return
        import json as _json
        from pathlib import Path as _Path
        sess_dir = _Path(self.context.path).parent
        meta_path = sess_dir / "session.json"
        try:
            meta = _json.loads(
                meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        except Exception:  # noqa: BLE001
            meta = {}
        total = meta.setdefault("usage_total", {})
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        output = int(usage.get("completion_tokens") or 0)
        total["cached_input"] = int(total.get("cached_input") or 0) + cached
        total["uncached_input"] = int(total.get("uncached_input") or 0) + (
            prompt - cached)
        total["output"] = int(total.get("output") or 0) + output
        # 冗余存最近一次原始 usage 便于核对
        total["last_usage"] = {
            "prompt_tokens": prompt, "cached_tokens": cached,
            "completion_tokens": output, "total_tokens": int(
                usage.get("total_tokens") or 0)}
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    async def _run_round(self, prepared: Optional[list[Event]] = None) -> bool:
        """Run one ReAct round. ``prepared`` events were written to context by
        ``run()`` before this call. Returns False on LLM failure."""
        self._round += 1
        round_id = self._round
        self._set_state(AgentState.STREAMING)

        # 本轮事件已写 context；这里仅构建 LLM 输入（失败重试去重）。
        known_ids = {e.get("event_id") for e in self.context.load()
                     if e.get("event_id")}
        incoming = [event.as_context_message() for event in (prepared or [])
                    if event.event_id not in known_ids]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}]
        messages.extend(self.context.messages_for_llm())
        messages.extend(incoming)
        messages = sanitize_tool_messages(messages)
        messages = append_trailing_empty_user(
            messages, self.trailing_empty_user)

        self._publish({"type": "round_started", "round_id": round_id})
        logger.info("round=%d start messages=%d tools=%d", round_id,
                    len(messages), len(self.tools.schemas()))

        def on_delta(channel: str, text: str) -> None:
            self._publish({"type": "token", "round_id": round_id,
                           "channel": channel, "delta": text})

        try:
            result = await self.llm.stream(
                messages, tools=self.tools.schemas(), on_delta=on_delta)
        except Exception as exc:  # noqa: BLE001
            logger.warning("round=%d FAILED: %s", round_id, exc)
            self._publish({"type": "round_failed", "round_id": round_id,
                           "error": str(exc)})
            self._set_state(AgentState.IDLE)
            return False

        logger.info("round=%d done tool_calls=%d usage=%s",
                    round_id, len(result.tool_calls),
                    json.dumps(result.usage or {}, ensure_ascii=False))

        assistant: dict[str, Any] = {"role": "assistant",
                                     "content": result.text}
        if result.usage:
            assistant["usage"] = result.usage
        if result.thinking:
            assistant["reasoning_content"] = result.thinking
        if result.provider_items:
            assistant["provider_items"] = result.provider_items
        if result.tool_calls:
            assistant["tool_calls"] = [
                {"id": c.call_id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments},
                 **({"signature": c.signature} if c.signature else {})}
                for c in result.tool_calls
            ]
        self.context.append(assistant)
        self._publish({"type": "context_appended", "message": assistant,
                       "round_id": round_id})
        # 额度统计：每轮 usage 落盘后累加三类 token 进 session 元数据
        if result.usage:
            self._accumulate_usage(result.usage)

        if result.tool_calls:
            interrupted = await self._run_tools(result.tool_calls, round_id)
        else:
            # 无工具调用 → 一轮 loop 结束，notice 通知用户完成结果
            interrupted = False
            self._publish({
                "type": "notice", "kind": "completed", "agent": self.name,
                "message": f"实例 {self.name} subagent 完成。",
                "model_output": result.text,
            })

        self._publish({"type": "round_finished", "round_id": round_id,
                       "has_tool_calls": bool(result.tool_calls)})
        self._set_state(AgentState.IDLE)
        return True

    async def _run_tools(self, calls: list[ToolCall], round_id: int) -> bool:
        """Execute tool calls with a status-code layer.

        状态码层（设计约定）：
        - 200 成功：结果 push 回事件队列，下一轮自动 call。
        - 中断类（403 拒绝 / 499 cancel）：本轮【全部】工具结果正常写入
          context（文件+内存），但【不 push 回队列】——不触发下一轮自动
          call，agent 回 IDLE 等外部输入（用户）。
        - 其他异常码（408 超时 / 500 失败等）：【可继续 call】——结果
          push 回队列正常推进；同时发 notice 事件（实例名+工具名+错误码），
          由 hub 经 notice 通道通知用户，subagent 侧可见错误码。

        Returns True when any INTERRUPT (403/499) occurred (blocked round).
        """
        self._set_state(AgentState.TOOL_USE)

        async def one(call: ToolCall) -> tuple[ToolCall, str, int]:
            logger.info("round=%d tool_start name=%s call_id=%s params=%s",
                        round_id, call.name, call.call_id,
                        json.dumps(call.params, ensure_ascii=False)[:200])
            self._publish({"type": "tool_started", "round_id": round_id,
                           "tool_call_id": call.call_id, "name": call.name,
                           "params": call.params})
            # 调用前统一钩子：记录工具日志（审计）。
            if self.tool_logger is not None:
                try:
                    self.tool_logger({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "tool": call.name,
                        "params": call.params,
                        "call_id": call.call_id,
                    })
                except Exception as exc:  # noqa: BLE001 — 日志失败不影响调用
                    logger.warning("tool_logger failed: %s", exc)
            started = time.monotonic()
            try:
                raw = await asyncio.wait_for(
                    self.tools.invoke(call.name, call.params),
                    timeout=self.tool_timeout_seconds)
                code, content = _read_tool_status(raw)
            except asyncio.TimeoutError:
                code, content = TOOL_STATUS_TIMEOUT, (
                    f"工具 {call.name} 超时"
                    f"（>{self.tool_timeout_seconds:g}s）")
            except Exception as exc:  # noqa: BLE001
                code, content = TOOL_STATUS_FAILED, (
                    f"工具 {call.name} 失败：{exc}")
            logger.info("round=%d tool_done name=%s code=%d elapsed=%.3fs",
                        round_id, call.name, code, time.monotonic() - started)
            self._publish({
                "type": "tool_finished", "round_id": round_id,
                "tool_call_id": call.call_id, "name": call.name,
                "elapsed": round(time.monotonic() - started, 3),
                "code": code, "content": content,
            })
            return call, content, code

        results = await asyncio.gather(*(one(c) for c in calls))
        # 中断判定：存在 403/499 → 整批【不 call】（写 context，不 push）。
        interrupted = any(code in INTERRUPT_CODES
                          for _call, _content, code in results)
        # 其他异常（非200且非中断）→ 可继续 call，但发 notice 让用户知道。
        error_notices = [
            (call.name, code) for call, _content, code in results
            if code != TOOL_STATUS_OK and code not in INTERRUPT_CODES
        ]

        for call, content, code in results:
            if self._stop.is_set():
                break
            if interrupted:
                # 中断轮：直接写 context（不唤醒队列），agent 停在 IDLE 不 call。
                self.context.append({
                    "role": "tool",
                    "content": content,
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "code": code,
                    "kind": "tool_result",
                    "source": "runtime",
                })
            else:
                self.push_event(Event(
                    role="tool",
                    content=content,
                    kind="tool_result",
                    source="runtime",
                    metadata={"tool_call_id": call.call_id, "name": call.name,
                              "code": code},
                ))

        if interrupted:
            codes = {call.name: code for call, _content, code in results}
            logger.warning(
                "round=%d tools INTERRUPTED (%s): %s", round_id,
                sorted(INTERRUPT_CODES), codes)
            self._publish({"type": "tools_interrupted", "round_id": round_id,
                           "codes": codes, "agent": self.name})
        elif error_notices:
            # 异常返回（可 call）→ notice 通知用户
            for tool_name, code in error_notices:
                self._publish({
                    "type": "notice", "kind": "tool_error", "agent": self.name,
                    "tool": tool_name, "code": code,
                    "message": f"实例 {self.name} 的工具 {tool_name} "
                               f"异常返回 code={code}，可继续执行。",
                })
        return interrupted