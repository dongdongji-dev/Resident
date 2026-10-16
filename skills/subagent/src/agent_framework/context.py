"""JSONL context store（subagent 从0重建版）。

``context.jsonl`` 是 agent 认知流的唯一真相源，按 session 分目录存储。
LLM 调用前会检查最近 ``repair_latest_n`` 条：无法解析的行丢弃、损坏的
assistant tool_calls 参数修复——否则后端会拒收请求。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.context")

REPAIR_NOTICE = ("系统请求前检查到上下文存在不合法条目，"
                 "可能原因：模型后端出错或内部系统问题")


def _is_valid_arguments(text: str) -> bool:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict)


def _repair_arguments(raw: Any) -> str:
    if isinstance(raw, str):
        for patch in ("}", '"}'):
            candidate = raw.rstrip() + patch
            if _is_valid_arguments(candidate):
                return candidate
        raw_text = raw
    else:
        raw_text = repr(raw)
    return json.dumps(
        {"repair_notice": REPAIR_NOTICE, "raw_arguments": raw_text},
        ensure_ascii=False)


def repair_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    changed = False
    if not isinstance(entry.get("content"), str):
        entry["content"] = str(entry.get("content", ""))
        changed = True
    if entry.get("role") != "assistant":
        return changed
    tool_calls = entry.get("tool_calls")
    if tool_calls is None:
        return changed
    if not isinstance(tool_calls, list):
        entry["tool_calls"] = []
        return True
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str) and _is_valid_arguments(arguments):
            continue
        function["arguments"] = _repair_arguments(arguments)
        changed = True
    if changed:
        entry["content"] = (
            f"{entry.get('content', '')}"
            f"\n\n[系统错误] {REPAIR_NOTICE}").strip()
    return changed


class ContextStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, message: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")

    def write_all(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("w", encoding="utf-8") as handle:
            for message in entries:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        return entries

    def has_event_id(self, event_id: str) -> bool:
        if not event_id:
            return False
        return any(e.get("event_id") == event_id for e in self.load())

    def messages_for_llm(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for entry in self.load():
            message = {"role": entry["role"],
                       "content": entry.get("content", "")}
            for key in ("reasoning_content", "tool_calls", "tool_call_id",
                        "name"):
                if entry.get(key):
                    message[key] = entry[key]
            messages.append(message)
        return messages

    def total_tokens(self) -> int:
        total = 0
        for entry in self.load():
            usage = entry.get("usage")
            if isinstance(usage, dict) and usage.get("total_tokens"):
                total = int(usage["total_tokens"])
        return total

    def repair_latest(self, n: int) -> tuple[int, int]:
        if not self.path.exists() or n <= 0:
            return (0, 0)
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            usable = [line for line in lines if line.strip()]
            if not usable:
                return (0, 0)
            tail = usable[-n:]
            repaired = 0
            dropped = 0
            out_tail: list[str] = []
            for line in tail:
                try:
                    entry = json.loads(line)
                except ValueError:
                    dropped += 1
                    logger.warning("context tail dropped unparseable line")
                    continue
                if repair_entry(entry):
                    repaired += 1
                    line = json.dumps(entry, ensure_ascii=False)
                out_tail.append(line)
            if repaired or dropped:
                non_tail = usable[:-n] if len(usable) > n else []
                self.path.write_text(
                    "\n".join(non_tail + out_tail) + "\n",
                    encoding="utf-8")
                logger.warning("context tail repaired=%d dropped=%d",
                               repaired, dropped)
        return (repaired, dropped)