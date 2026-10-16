"""Notices: once / cron scheduled notices. Delivered to the Agent by the
beat cycle; this module is storage + due-detection only.

Time format follows the project rule: local time ``YYYY-MM-DD HH:MM:SS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from kernel.scheduler.cron import cron_match, is_valid_cron
from kernel.osdb.db import Database
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.timeutil import now_str

logger = get_logger("kernel.osdb.notice")

NOTICE_SCHEMA: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "kind": "TEXT NOT NULL DEFAULT 'once'",      # once（一次性）/ cron（循环）
    "due_at": "TEXT NOT NULL DEFAULT ''",        # once：触发时间（本地时间）
    "cron": "TEXT NOT NULL DEFAULT ''",          # cron：crontab 表达式
    "content": "TEXT NOT NULL",                  # 完整通知内容
    "notification": "TEXT NOT NULL DEFAULT ''",  # 短通知（优先推送）
    "done": "INTEGER NOT NULL DEFAULT 0",        # once：是否已消费
    "last_run_at": "TEXT NOT NULL DEFAULT ''",   # cron：上次执行分钟（防重复）
    "created_at": "TEXT NOT NULL DEFAULT ''",
}

NOTICE_COLUMNS = ("id", "kind", "due_at", "cron", "content", "notification",
                  "done", "last_run_at", "created_at")


@dataclass(slots=True)
class Notice:
    notice_id: int
    kind: str
    due_at: str
    cron: str
    content: str
    notification: str
    done: bool
    last_run_at: str
    created_at: str

    def push_text(self) -> str:
        return f"[通知 #{self.notice_id}] {self.notification or self.content}"


class NoticeStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        db.create_table("notice", NOTICE_SCHEMA)
        db.commit()

    def create(self, content: str, kind: str = "once",
               due_at: str = "", delay_seconds: int = 0,
               cron: str = "", notification: str = "") -> int:
        """Create a notice; returns the notice id."""
        content = str(content or "").strip()
        kind = str(kind or "once").strip().lower()
        if not content:
            raise ValueError("content（通知内容）不能为空")
        if kind not in ("once", "cron"):
            raise ValueError(f"kind 只能是 once 或 cron，收到: {kind!r}")
        if kind == "once":
            if str(due_at or "").strip():
                due = str(due_at).strip()
                try:
                    datetime.strptime(due, "%Y-%m-%d %H:%M:%S")
                except ValueError as exc:
                    raise ValueError(
                        f"due_at 格式错误: {due!r}（应为 YYYY-MM-DD HH:MM:SS）"
                    ) from exc
            else:
                moment = datetime.now()
                due = moment.fromtimestamp(
                    moment.timestamp() + max(0, int(delay_seconds))
                ).strftime("%Y-%m-%d %H:%M:%S")
            cron_expr = ""
        else:
            cron_expr = str(cron or "").strip()
            if not cron_expr:
                raise ValueError("cron 任务必须提供 crontab 表达式（分 时 日 月 周）")
            if not is_valid_cron(cron_expr):
                raise ValueError(f"cron 表达式无效: {cron_expr!r}")
            due = ""
        notice_id = self._db.execute_returning(
            "INSERT INTO notice (kind, due_at, cron, content, notification,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, due, cron_expr, content, str(notification or ""),
             now_str()))
        self._db.commit()
        logger.info("notice created id=%d kind=%s due=%s cron=%s",
                    notice_id, kind, due or "-", cron_expr or "-")
        return notice_id

    def delete(self, notice_id: int) -> bool:
        row = self._db.query_one("SELECT id FROM notice WHERE id = ?",
                                 (int(notice_id),))
        if row is None:
            return False
        self._db.execute("DELETE FROM notice WHERE id = ?", (int(notice_id),))
        self._db.commit()
        logger.info("notice deleted id=%d", int(notice_id))
        return True

    def list_cron(self) -> list[Notice]:
        """所有循环任务（kind = cron），按 id 升序。"""
        rows = self._db.query(
            "SELECT " + ", ".join(NOTICE_COLUMNS)
            + " FROM notice WHERE kind = 'cron' ORDER BY id")
        return [self._row_to_notice(row) for row in rows]

    def list(self, include_done: bool = False) -> list[Notice]:
        sql = ("SELECT " + ", ".join(NOTICE_COLUMNS) + " FROM notice")
        if not include_done:
            sql += " WHERE done = 0"
        sql += " ORDER BY id"
        return [self._row_to_notice(row) for row in self._db.query(sql)]

    def query(self, notice_id: Optional[int] = None, day: str = "",
              include_done: bool = False, limit: int = 20) -> list[Notice]:
        """查询通知（V2 query_plans 语义）：
        notice_id 单查（不过滤 done）；day 按 due_at/last_run_at 日期；
        include_done 过滤；limit 上限。"""
        where: list[str] = []
        params: list[Any] = []
        if notice_id is not None and int(notice_id) > 0:
            where.append("id = ?")
            params.append(int(notice_id))
        if str(day or "").strip():
            where.append("(date(due_at) = ? OR date(last_run_at) = ?)")
            params += [str(day).strip(), str(day).strip()]
        if not include_done and notice_id is None:
            where.append("done = 0")
        sql = "SELECT " + ", ".join(NOTICE_COLUMNS) + " FROM notice"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        rows = self._db.query(sql, tuple(params))
        return [self._row_to_notice(row) for row in rows]

    def get(self, notice_id: int) -> Optional[Notice]:
        row = self._db.query_one(
            "SELECT " + ", ".join(NOTICE_COLUMNS)
            + " FROM notice WHERE id = ?", (int(notice_id),))
        return self._row_to_notice(row) if row is not None else None

    def due(self, now: Optional[datetime] = None) -> list[Notice]:
        """Notices due at this moment (atomic read + mark):
        - once: due_at <= now, single fire (done=1);
        - cron: crontab matches the current minute, guarded by last_run_at.
        Returns the due notices; the caller turns them into Agent events."""
        moment = now or datetime.now()
        now_str_value = moment.strftime("%Y-%m-%d %H:%M:%S")
        minute_key = moment.strftime("%Y-%m-%d %H:%M")
        due: list[Notice] = []
        for row in self._db.query(
                "SELECT " + ", ".join(NOTICE_COLUMNS)
                + " FROM notice WHERE done = 0"):
            notice = self._row_to_notice(row)
            if notice.kind == "cron":
                if notice.cron and cron_match(notice.cron, moment) \
                        and minute_key != notice.last_run_at:
                    self._db.execute(
                        "UPDATE notice SET last_run_at = ? WHERE id = ?",
                        (minute_key, notice.notice_id))
                    due.append(notice)
                continue
            if notice.due_at and notice.due_at <= now_str_value:
                self._db.execute(
                    "UPDATE notice SET done = 1 WHERE id = ?",
                    (notice.notice_id,))
                due.append(notice)
        if due:
            self._db.commit()
        return due

    @staticmethod
    def _row_to_notice(row: Any) -> Notice:
        return Notice(
            notice_id=int(row["id"]),
            kind=str(row["kind"]),
            due_at=str(row["due_at"]),
            cron=str(row["cron"]),
            content=str(row["content"]),
            notification=str(row["notification"]),
            done=bool(row["done"]),
            last_run_at=str(row["last_run_at"]),
            created_at=str(row["created_at"] or ""),
        )