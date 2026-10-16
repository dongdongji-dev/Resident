"""Agent-facing notice tools: create / list / delete (aligned with the
POST /api/notice HTTP endpoints; the Beat delivers due notices)."""

from __future__ import annotations

import os
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.paths import WorkspacePath
from kernel.osdb.notice import NoticeStore

logger = get_logger("kernel.agent_framework.notice_tools")


def _save_cutoff(workspace: WorkspacePath, text: str, prefix: str,
                 truncated_dir: str) -> Optional[str]:
    """Persist full (would-be-truncated-away) content into the workspace
    cache dir; returns the container-side path (None on failure)."""
    if not text or not truncated_dir:
        return None
    try:
        directory = os.path.abspath(truncated_dir)
        os.makedirs(directory, exist_ok=True)
        import time as _time
        import uuid as _uuid
        filename = (f"{prefix}_{_time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{_uuid.uuid4().hex[:6]}.txt")
        host = os.path.join(directory, filename)
        with open(host, "w", encoding="utf-8") as handle:
            handle.write(str(text))
        return workspace.to_container(host)
    except Exception:  # noqa: BLE001
        return None


def make_notice_create(notices: NoticeStore) -> Any:
    def create(content: str, kind: str = "once", due_at: str = "",
               delay_seconds: int = 0, cron: str = "",
               notification: str = "") -> dict[str, Any]:
        """创建一个通知，到期时由 beat 推送给 agent。

        参数:
          content: 通知完整内容（必填）。
          kind: once（一次性，默认）/ cron（循环）。cron 需提供 crontab
            表达式（分 时 日 月 周）。
          due_at: once 的触发时间 "YYYY-MM-DD HH:MM:SS"（与 delay_seconds
            二选一；都缺省则立即触发）。
          delay_seconds: once 的延迟秒数（从当前时间起算）。
          cron: cron 的 crontab 表达式。
          notification: 可选短通知（到期优先展示，短于 content）。

        用途: 自主安排未来任务——延时提醒、定时检查、定期汇报等。
        返回创建的 notice_id 与状态。
        """
        notice_id = notices.create(
            content=str(content or ""),
            kind=str(kind or "once"),
            due_at=str(due_at or ""),
            delay_seconds=int(delay_seconds or 0),
            cron=str(cron or ""),
            notification=str(notification or ""),
        )
        logger.info("notice_create id=%d kind=%s", notice_id,
                    str(kind or "once"))
        return {
            "notice_id": notice_id,
            "kind": str(kind or "once"),
            "status": "pending",
            "hint": "到期时由 beat 推送提醒；可用 notice_delete 取消",
        }

    return create


def make_notice_list(notices: NoticeStore, max_results: int = 20,
                     preview_chars: int = 200,
                     workspace: Optional[WorkspacePath] = None,
                     truncated_dir: str = "",
                     runtime_path: Any = None) -> Any:
    def list_notices(notice_id: int = 0, day: str = "",
                     include_done: bool = False, limit: int = 20) -> dict[str, Any]:
        """列出/查询通知。

        参数:
          notice_id: 按通知 id 精确查询（含已完成；返回该通知完整内容）。
          day: 按日期查询（YYYY-MM-DD，匹配 due_at 或 last_run_at）。
          include_done: true 时包含已完成通知（默认只列未完成）。
          limit: 最多返回条数（默认 20，上限 100）。

        用途: 查看已安排的通知（id/内容/触发时间/是否完成），配合
        notice_delete 清理；结果较多时 content 裁剪，完整内容用
        notice_id 单查。
        """
        limit = max(1, min(int(limit or 20), 100))
        rows = notices.query(
            notice_id=int(notice_id or 0) if notice_id else None,
            day=str(day or ""), include_done=bool(include_done), limit=limit)
        trim = len(rows) > max_results
        items = []
        full_blocks: list[str] = []
        for n in rows:
            content = str(n.content)
            if trim and len(content) > preview_chars:
                content = (content[:preview_chars]
                           + "…（内容过长已裁剪，可用 notice_list(notice_id=N) "
                             "查看完整内容）")
            if trim and len(content) > preview_chars:
                full_blocks.append(f"[通知 #{n.notice_id}]\n{n.content}")
            items.append({"notice_id": n.notice_id, "kind": n.kind,
                          "due_at": n.due_at, "cron": n.cron,
                          "content": content,
                          "notification": n.notification,
                          "done": n.done, "last_run_at": n.last_run_at})
        result = {"count": len(items), "notices": items}
        if full_blocks and workspace is not None and truncated_dir:
            path = _save_cutoff(workspace, "\n\n---\n\n".join(full_blocks),
                                "notices_full", truncated_dir)
            if path:
                # 统一运行时可读路径（沙箱=容器内 realpath；见 runtime_path）
                if runtime_path is not None:
                    try:
                        path = runtime_path(path)
                    except Exception:  # noqa: BLE001 — 映射失败时保留原路径
                        pass
                result["full_content_file"] = path
        return result

    return list_notices


def make_notice_list_cron(notices: NoticeStore) -> Any:
    def list_cron() -> dict[str, Any]:
        """列出 notice 表中的所有循环任务（kind=cron）。

        用途: 查看当前安排了哪些定期任务（crontab 表达式/内容/下次与
        上次执行时间），配合 notice_delete 调整。只返回 kind=cron 的
        记录，一次性通知（kind=once）不在其中（用 notice_list 查看）。
        """
        rows = notices.list_cron()
        items = [{"notice_id": n.notice_id, "kind": n.kind,
                  "due_at": n.due_at, "cron": n.cron,
                  "content": n.content, "notification": n.notification,
                  "done": n.done, "last_run_at": n.last_run_at}
                 for n in rows]
        return {"count": len(items), "cron_notices": items}

    return list_cron


def make_notice_delete(notices: NoticeStore) -> Any:
    def delete_notice(notice_id: int) -> dict[str, Any]:
        """删除通知。

        参数:
          notice_id: 通知 id（notice_list 返回）。

        用途: 取消不再需要的通知。
        """
        ok = notices.delete(int(notice_id))
        return {"ok": ok, "notice_id": int(notice_id),
                "deleted": ok,
                "error": "" if ok else f"通知 {notice_id} 不存在"}

    return delete_notice