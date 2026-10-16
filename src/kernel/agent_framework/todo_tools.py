"""Agent-facing todo tools (todo_update / todo_list) over TodoStore."""

from __future__ import annotations

from typing import Any, Optional

from kernel.osdb.todos import TodoStore


def make_todo_update(store: TodoStore) -> Any:
    def todo_update(tasks: Optional[list[dict[str, Any]]] = None,
                    updates: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """新增/更新 todo（合并工具）。

        参数:
          tasks: 添加/追加 todo 列表。每项 {"task": 任务名,
            "todos": [待办文案]}。task 不存在则新建，已存在则向该 task
            追加 todos（按文案去重，新增项默认未完成）。
          updates: 批量更新不同 task 的 todo 状态。每项 {"task": 任务名,
            "todos": [待办文案], "status": "completed"|"pending"}。当某
            task 的全部 todo 均标记为 completed([x]) 时，该 task 视为完成
            并从 todo 树移除（返回信息里带 removed）。
        """
        result: dict[str, Any] = {"ok": True}
        if tasks:
            result["add"] = store.add_tasks(tasks)
        if updates:
            result["update"] = store.apply_updates(updates)
        if not tasks and not updates:
            raise ValueError("tasks 或 updates 至少提供一个")
        return result

    return todo_update


def make_todo_list(store: TodoStore) -> Any:
    def todo_list() -> str:
        """列出整个 todo 树。

        输出 Markdown：# Todo 下按 task 分组（## task），每个 task 下列出
        其待办及状态（- [x] 已完成 / - [ ] 未完成）。todo 树只有两层：
        task 及其 todos，不会再嵌套。
        """
        if store.is_empty():
            return "# Todo\n（暂无 todo）"
        return store.markdown()

    return todo_list