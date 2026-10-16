"""Persistent todo tree: task -> list of todo items (each with done state).

The todo tree is a flat two-level structure only — a task holds its todos,
and todos never nest deeper. The store persists to a JSON file (path from
config ``todos.file``) so todos survive restarts.

Write semantics:

- add (``tasks``): create the task when missing, otherwise append todos
  (deduped by text, new items start pending).
- update (``updates``): flip matching todos to completed/pending; when every
  todo of a task becomes ``[x]`` the task is considered finished and is
  REMOVED from the tree (the removal is reported back to the caller).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.osdb.todos")

_VALID_STATUS = ("completed", "pending")


class TodoStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._tree: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (ValueError, OSError) as exc:
            logger.warning("todo load failed, starting empty: %s", exc)
            return
        if isinstance(data, dict):
            self._tree = data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self._tree, handle, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def is_empty(self) -> bool:
        with self._lock:
            return not bool(self._tree)

    def tree(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._tree))

    def add_tasks(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Create missing tasks / append todos (deduped by text)."""
        with self._lock:
            created: list[str] = []
            appended: dict[str, int] = {}
            for item in tasks or []:
                task = str(item.get("task", "")).strip()
                if not task:
                    raise ValueError("task 不能为空")
                todos = [str(t).strip() for t in (item.get("todos") or [])
                         if str(t).strip()]
                if task not in self._tree:
                    self._tree[task] = []
                    created.append(task)
                existing = {entry["text"] for entry in self._tree[task]}
                added = [text for text in todos if text not in existing]
                for text in added:
                    self._tree[task].append({"text": text, "done": False})
                appended[task] = len(added)
            self._save()
        return {
            "ok": True,
            "created": created,
            "appended_todos": appended,
            "total_tasks": len(self._tree),
        }

    def apply_updates(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        """Flip todo statuses; remove tasks whose every todo is done."""
        with self._lock:
            removed: list[str] = []
            updated: list[dict[str, Any]] = []
            for item in updates or []:
                task = str(item.get("task", "")).strip()
                status = str(item.get("status", "")).strip()
                if status not in _VALID_STATUS:
                    raise ValueError(
                        f"status 必须是 {'/'.join(_VALID_STATUS)}，收到 {status!r}")
                todos = {str(t).strip() for t in (item.get("todos") or [])
                         if str(t).strip()}
                if not task:
                    raise ValueError("task 不能为空")
                if task not in self._tree:
                    raise ValueError(f"task 不存在：{task!r}")
                done = status == "completed"
                count = 0
                for entry in self._tree[task]:
                    if entry["text"] in todos and entry["done"] != done:
                        entry["done"] = done
                        count += 1
                updated.append(
                    {"task": task, "status": status, "count": count})
                if self._tree[task] and all(
                        entry["done"] for entry in self._tree[task]):
                    del self._tree[task]
                    removed.append(task)
            self._save()
        return {
            "ok": True,
            "updated": updated,
            "removed": removed,
            "total_tasks": len(self._tree),
        }

    def markdown(self) -> str:
        """Render the whole tree as ``# Todo`` / ``## task`` / ``- [x] todo``."""
        with self._lock:
            lines = ["# Todo"]
            for task, todos in self._tree.items():
                lines.append(f"## {task}")
                for entry in todos:
                    box = "[x]" if entry.get("done") else "[ ]"
                    lines.append(f"- {box} {entry['text']}")
            return "\n".join(lines)