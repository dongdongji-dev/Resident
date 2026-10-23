"""JSONL context store + tail repair.

``context.jsonl`` is the single source of truth for the agent's cognition
stream. Before every LLM call the Agent checks the LATEST ``repair_latest_n``
entries: unparseable lines are dropped and assistant tool-calls with broken
``function.arguments`` are repaired (closed when trivially possible,
otherwise replaced by a valid error payload) — the backend would reject the
request otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.context")

REPAIR_NOTICE = ("系统请求前检查到上下文存在不合法条目，"
                 "可能原因：模型后端出错或内部系统问题")


def _local_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _content_chars(content: Any) -> int:
    """Character count of one context entry's content (str directly; block
    lists via their JSON serialization). Image blocks are EXCLUDED — a
    base64 data URL is ~1.3 chars per byte and would count as tens of
    thousands of fake tokens (local/readimage mode); images are accounted
    separately via ``image_tokens`` in ``estimated_tokens``."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image_url":
                    continue
                text_value = block.get("text")
                if isinstance(text_value, str):
                    total += len(text_value)
                    continue
            try:
                total += len(json.dumps(block, ensure_ascii=False))
            except (TypeError, ValueError):
                total += len(str(block))
        return total
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(content))


def _content_images(content: Any) -> int:
    """Number of image blocks in one entry's content."""
    if not isinstance(content, list):
        return 0
    return sum(1 for block in content
               if isinstance(block, dict) and block.get("type") == "image_url")


def _is_valid_arguments(text: str) -> bool:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict)


def _repair_arguments(raw: Any) -> str:
    """Close an unterminated arguments JSON when possible; otherwise wrap the
    raw text in a valid error payload so the message stays sendable."""
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
        ensure_ascii=False,
    )


def repair_entry(entry: dict[str, Any]) -> bool:
    """Repair one context entry in place; return True when changed."""
    if not isinstance(entry, dict):
        return False
    changed = False
    content = entry.get("content")
    if content is None:
        entry["content"] = ""
        changed = True
    elif not isinstance(content, (str, list)):
        # 图片等多模态内容以 block 列表保存，绝不能被 str() 毁掉
        entry["content"] = str(content)
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
        """Atomic full rewrite: write a temp file first, then replace, so a
        crash mid-write cannot truncate/corrupt the existing context."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with self._lock, tmp.open("w", encoding="utf-8") as handle:
            for message in entries:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def load(self) -> list[dict[str, Any]]:
        """Valid entries only; unparseable lines are skipped (not fatal)."""
        if not self.path.exists():
            return []
        with self._lock:
            entries: list[dict[str, Any]] = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        return entries

    def size_bytes(self) -> int:
        """Current on-disk size of context.jsonl (0 when absent) — cheap
        proxy for the request body bytes the context contributes."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def has_event_id(self, event_id: str) -> bool:
        """True when an entry with the given event_id already exists (used to
        dedupe re-enqueued events across a failed-round retry)."""
        if not event_id:
            return False
        return any(e.get("event_id") == event_id for e in self.load())

    def messages_for_llm(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for entry in self.load():
            message = {"role": entry["role"],
                       "content": entry.get("content", "")}
            # reasoning chain preserved so the model keeps its own history;
            # empty tool_calls arrays are stripped (some backends 400 on them)
            for key in ("reasoning_content", "tool_calls", "tool_call_id",
                        "name"):
                if entry.get(key):
                    message[key] = entry[key]
            messages.append(message)
        return messages

    def total_tokens(self) -> int:
        """Context footprint = total_tokens of the newest assistant entry."""
        total = 0
        for entry in self.load():
            usage = entry.get("usage")
            if isinstance(usage, dict) and usage.get("total_tokens"):
                total = int(usage["total_tokens"])
        return total

    def estimated_tokens(self, chars_per_token: float,
                         image_tokens: int = 0) -> int:
        """Footprint for the compression trigger: the newest provider-reported
        ``usage.total_tokens`` PLUS an estimate of every entry appended AFTER
        that assistant entry (text chars / ``chars_per_token``; each image
        block counts ``image_tokens`` — base64 is never counted as text).
        The provider number alone lags behind the context tail: a huge tool
        result appended after the last successful round is invisible to the
        trigger, so the round keeps failing with context_length_exceeded
        without ever compressing (2026-10-11 incident)."""
        if chars_per_token <= 0:
            chars_per_token = 1.0
        entries = self.load()
        base = 0
        base_index = -1
        for index, entry in enumerate(entries):
            usage = entry.get("usage")
            if isinstance(usage, dict) and usage.get("total_tokens"):
                base = int(usage["total_tokens"])
                base_index = index
        tail = entries[base_index + 1:] if base_index >= 0 else entries
        chars = 0
        images = 0
        for entry in tail:
            content = entry.get("content")
            chars += _content_chars(content)
            images += _content_images(content)
        return (base
                + int((chars + chars_per_token - 1) // chars_per_token)
                + images * max(0, int(image_tokens)))

    def content_tokens(self, chars_per_token: float,
                       image_tokens: int = 0) -> int:
        """Provider-usage-free footprint (text chars / ``chars_per_token``
        + images * ``image_tokens``). Used right after a compression, when
        the newest provider usage still describes the PRE-compression
        context and would otherwise trigger the same compression again."""
        if chars_per_token <= 0:
            chars_per_token = 1.0
        chars = 0
        images = 0
        for entry in self.load():
            content = entry.get("content")
            chars += _content_chars(content)
            images += _content_images(content)
        return (int((chars + chars_per_token - 1) // chars_per_token)
                + images * max(0, int(image_tokens)))

    def repair_latest(self, n: int) -> tuple[int, int]:
        """Check the LATEST n entries; returns (repaired, dropped).
        Unparseable tail lines are dropped; valid-but-malformed entries are
        repaired in place. Rewrites the file once when anything changed."""
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

    def archive(self, archive_dir: str | Path) -> Optional[str]:
        """Move current jsonl to ``archive_dir`` with a timestamped name."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        target_dir = Path(archive_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = _local_now().replace(":", "").replace(" ", "-")
        target = target_dir / f"context-{stamp}.jsonl"
        with self._lock:
            os.replace(self.path, target)
        logger.info("context archived: %s", target)
        return str(target)

    def copy_archive(self, archive_dir: str | Path) -> Optional[str]:
        """Copy current jsonl to ``archive_dir`` with a timestamped name,
        KEEPING the original in place. Compression uses this so a crash or
        kill mid-compression can never leave the active context missing
        (the old implementation moved the file away first)."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        target_dir = Path(archive_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = _local_now().replace(":", "").replace(" ", "-")
        target = target_dir / f"context-{stamp}.jsonl"
        with self._lock:
            shutil.copyfile(self.path, target)
        logger.info("context archived (copy): %s", target)
        return str(target)

    def find_keep_start(self, keep_rounds: int) -> int:
        """Index where the recent-window starts (count user->assistant
        rounds from the bottom, extend up across a user batch)."""
        entries = self.load()
        if not entries:
            return 0
        i = len(entries) - 1
        rounds = 0
        while i >= 0:
            role = entries[i].get("role")
            if role == "user":
                rounds += 1
                if rounds >= keep_rounds:
                    break
                i -= 1
                while i >= 0 and entries[i].get("role") != "assistant":
                    i -= 1
            i -= 1
        start = max(0, i)
        while start > 0 and entries[start - 1].get("role") == "user":
            start -= 1
        return start
