"""Message data layer: messages + messages_archive stores (identity info
lives in ``sender_data`` JSON rather than separate identity tables).

- ``conversation_key`` is SERVER-generated: ``platform:conversation_id:
  remote_message_id`` (unique per message; dedupe on insert).
- ``sent_to_llm``: 0 = unread, 1 = already consumed. The unread set is
  ``direction='in' AND sent_to_llm=0`` (the wake-up source).
- ``messages_archive`` is a same-shape table; ``archive_expired`` moves
  consumed, older-than-N-days messages there (unread never archived).

Business rules (wake filtering, outbound routing) live in later nodes; this
module is the storage + query layer only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from kernel.osdb.db import Database
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.timeutil import now_str

logger = get_logger("kernel.osdb.messages")

MESSAGES_SCHEMA: dict[str, str] = {
    "seq": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "platform": "TEXT NOT NULL",
    "conversation_id": "TEXT NOT NULL",
    "remote_message_id": "TEXT NOT NULL",
    "conversation_key": "TEXT NOT NULL",
    "conversation_type": "TEXT NOT NULL DEFAULT ''",
    "message_type": "TEXT NOT NULL DEFAULT 'normal'",
    "sender_data": "TEXT NOT NULL DEFAULT '{}'",
    "content": "TEXT NOT NULL DEFAULT ''",
    "llm_content": "TEXT NOT NULL DEFAULT ''",
    "raw_segments": "TEXT",
    "direction": "TEXT NOT NULL DEFAULT 'in'",
    "wakeup": "INTEGER NOT NULL DEFAULT 0",
    "sent_to_llm": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "TEXT NOT NULL",
}

MESSAGE_COLUMNS = ("seq", "platform", "conversation_id", "remote_message_id",
                   "conversation_key", "conversation_type", "message_type",
                   "sender_data", "content", "llm_content", "raw_segments",
                   "direction", "wakeup", "sent_to_llm", "created_at")


def message_to_dict(row: Any) -> dict[str, Any]:
    """Row -> dict with JSON/bool decoding (API-facing shape)."""
    data = {col: row[col] for col in MESSAGE_COLUMNS}
    data["wakeup"] = bool(data["wakeup"])
    data["sent_to_llm"] = bool(data["sent_to_llm"])
    try:
        data["sender_data"] = json.loads(data["sender_data"] or "{}")
    except (TypeError, ValueError):
        data["sender_data"] = {}
    try:
        data["raw_segments"] = json.loads(data["raw_segments"])
    except (TypeError, ValueError):
        data["raw_segments"] = None
    return data


class MessagesStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        db.create_table("messages", MESSAGES_SCHEMA)
        db.commit()

    # ── write ──────────────────────────────────────────────────────────

    def insert(self, *, platform: str, conversation_id: str,
               conversation_type: str = "", message_type: str = "normal",
               remote_message_id: str,
               sender_data: Optional[dict[str, Any]] = None,
               content: str = "", llm_content: str = "",
               raw_segments: Optional[list[Any]] = None,
               direction: str = "in", wakeup: bool = False,
               sent_to_llm: int = 0,
               created_at: str = "") -> Optional[dict[str, Any]]:
        """Insert one message. Returns the stored row, or None when the
        conversation_key already exists (deduped)."""
        platform = str(platform or "").strip()
        conversation_id = str(conversation_id or "").strip()
        remote_message_id = str(remote_message_id or "").strip()
        if not platform or not conversation_id or not remote_message_id:
            raise ValueError(
                "platform、conversation_id、remote_message_id 不能为空")
        direction = str(direction or "in").strip()
        if direction not in ("in", "out"):
            raise ValueError(f"direction 只能是 in 或 out，收到: {direction!r}")
        conversation_key = f"{platform}:{conversation_id}:{remote_message_id}"

        now = created_at or now_str()
        message_type = str(message_type or "normal")
        existing = self._db.query_one(
            "SELECT seq FROM messages WHERE conversation_key = ?"
            " AND message_type = ?", (conversation_key, message_type))
        if existing is not None:
            logger.info("message deduped key=%s type=%s",
                        conversation_key, message_type)
            return None
        # out 消息（bot 自己发出）入库即已读：无需被唤醒读取
        sent_to_llm = 1 if direction == "out" else int(bool(sent_to_llm))
        self._db.execute(
            "INSERT INTO messages (platform, conversation_id,"
            " remote_message_id, conversation_key, conversation_type,"
            " message_type, sender_data, content, llm_content,"
            " raw_segments, direction, wakeup, sent_to_llm, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, conversation_id, remote_message_id, conversation_key,
             str(conversation_type or ""), message_type,
             json.dumps(dict(sender_data or {}), ensure_ascii=False),
             str(content or ""), str(llm_content or ""),
             json.dumps(raw_segments or [], ensure_ascii=False)
             if raw_segments else None,
             direction, 1 if wakeup else 0, sent_to_llm, now))
        self._db.commit()
        return self.get_by_key(conversation_key)

    def mark_sent_to_llm(self, conversation_keys: list[str]) -> int:
        """Mark consumed messages as read (wake packing / tool queries)."""
        keys = [str(k) for k in conversation_keys]
        if not keys:
            return 0
        placeholders = ", ".join("?" for _ in keys)
        self._db.execute(
            "UPDATE messages SET sent_to_llm = 1 WHERE conversation_key IN ("
            + placeholders + ")", tuple(keys))
        self._db.commit()
        return int(self._db.query_one("SELECT changes() AS n")["n"])

    def mark_conversation_read(self, thread: str) -> int:
        """Mark all unread in-bound messages of a conversation as read."""
        platform, conversation_id = _parse_thread(thread)
        self._db.execute(
            "UPDATE messages SET sent_to_llm = 1"
            " WHERE platform = ? AND conversation_id = ?"
            " AND direction = 'in' AND sent_to_llm = 0",
            (platform, conversation_id))
        self._db.commit()
        return int(self._db.query_one("SELECT changes() AS n")["n"])

    def patch(self, conversation_key: str,
              fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Update whitelisted mutable fields of one message.
        Whitelist: llm_content / raw_segments / wakeup / sent_to_llm."""
        row = self._db.query_one(
            "SELECT seq FROM messages WHERE conversation_key = ?",
            (str(conversation_key),))
        if row is None:
            return None
        allowed = {"llm_content", "raw_segments", "wakeup", "sent_to_llm"}
        updates = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"字段不可修改: {key!r}（白名单: {sorted(allowed)}）")
            if key in ("wakeup", "sent_to_llm"):
                value = 1 if value else 0
            elif key == "raw_segments":
                value = (json.dumps(value, ensure_ascii=False)
                         if value is not None else None)
            else:
                value = str(value)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return self.get_by_key(conversation_key)
        params.append(str(conversation_key))
        self._db.execute(
            "UPDATE messages SET " + ", ".join(updates)
            + " WHERE conversation_key = ?", tuple(params))
        self._db.commit()
        return self.get_by_key(conversation_key)

    # ── read ───────────────────────────────────────────────────────────

    def get_by_key(self, conversation_key: str) -> Optional[dict[str, Any]]:
        row = self._db.query_one(
            "SELECT " + ", ".join(MESSAGE_COLUMNS)
            + " FROM messages WHERE conversation_key = ?",
            (str(conversation_key),))
        return message_to_dict(row) if row is not None else None

    def get_by_remote(self, platform: str,
                      remote_message_id: str) -> Optional[dict[str, Any]]:
        row = self._db.query_one(
            "SELECT " + ", ".join(MESSAGE_COLUMNS)
            + " FROM messages WHERE platform = ? AND remote_message_id = ?",
            (str(platform), str(remote_message_id)))
        return message_to_dict(row) if row is not None else None

    def conversation(self, platform: str, conversation_id: str,
                     after_seq: int = 0, limit: int = 100,
                     direction: str = "all") -> list[dict[str, Any]]:
        """Messages of one conversation (seq ascending, incremental cursor)."""
        clauses = ["platform = ?", "conversation_id = ?", "seq > ?"]
        params: list[Any] = [platform, conversation_id, int(after_seq)]
        if direction in ("in", "out"):
            clauses.append("direction = ?")
            params.append(direction)
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages WHERE "
            + " AND ".join(clauses) + " ORDER BY seq LIMIT ?",
            tuple(params) + (int(limit),))
        return [message_to_dict(row) for row in rows]

    def pending(self, limit: int = 500, window_hours: float = 0,
                max_per_session: int = 0) -> list[dict[str, Any]]:
        """Unread in-bound messages (the wake-up source set).

        ``window_hours`` > 0: older unread is TREATED AS READ (excluded) —
        it neither wakes nor is counted. ``max_per_session`` > 0: per
        conversation, only the newest N unread count (older ones treated
        as read). Urgent (wakeup=1) messages are NOT filtered by the window
        (they must not wait); the session cap still applies to them.
        """
        clauses = ["sent_to_llm = 0", "direction = 'in'"]
        params: list[Any] = []
        if window_hours > 0:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = (_dt.now() - _td(hours=window_hours)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            clauses.append("(created_at >= ? OR wakeup = 1)")
            params.append(cutoff)
        where = " AND ".join(clauses)
        if max_per_session > 0:
            # 每会话取最新 N 条（更早的视为已读）
            rows = self._db.query(
                "SELECT * FROM (SELECT " + ", ".join(MESSAGE_COLUMNS) + ","
                " ROW_NUMBER() OVER (PARTITION BY platform, conversation_id"
                " ORDER BY seq DESC) AS rn FROM messages WHERE " + where
                + ") WHERE rn <= ? ORDER BY seq LIMIT ?",
                tuple(params) + (int(max_per_session), int(limit)))
            return [message_to_dict(row) for row in rows]
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS)
            + " FROM messages WHERE " + where
            + " ORDER BY seq LIMIT ?", tuple(params) + (int(limit),))
        return [message_to_dict(row) for row in rows]

    def unread_count(self, platform: str = "",
                     conversation_id: str = "") -> int:
        clauses = ["sent_to_llm = 0", "direction = 'in'"]
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE "
            + " AND ".join(clauses), tuple(params))
        return int(row["n"])

    # ── thread queries（会话 = platform:conversation_id）────────────────

    def thread_stats(self, thread: str) -> dict[str, Any]:
        """单会话统计：消息总数 + 最新 seq。"""
        platform, conversation_id = _parse_thread(thread)
        row = self._db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(MAX(seq), 0) AS latest_seq"
            " FROM messages WHERE platform = ? AND conversation_id = ?",
            (platform, conversation_id))
        return {"thread": thread, "count": int(row["n"]),
                "latest_seq": int(row["latest_seq"])}

    def threads(self, limit: int = 100,
                platform: str = "") -> list[dict[str, Any]]:
        """所有出现过的会话（platform:conversation_id，主表 + 归档表合并），
        按最新消息 seq 降序。Returns [{thread, count, latest_seq,
        latest_at}]."""
        clauses = ""
        params: list[Any] = []
        if platform:
            clauses = " WHERE platform = ?"
            params = [str(platform)]
        self._ensure_archive()
        rows = self._db.query(
            "SELECT platform, conversation_id, COUNT(*) AS n,"
            " MAX(seq) AS latest_seq, MAX(created_at) AS latest_at"
            " FROM (SELECT platform, conversation_id, seq, created_at"
            " FROM messages UNION ALL"
            " SELECT platform, conversation_id, seq, created_at"
            " FROM messages_archive)"
            + clauses + " GROUP BY platform, conversation_id"
            + " ORDER BY latest_seq DESC LIMIT ?",
            tuple(params) + (int(limit),))
        return [{
            "thread": f"{row['platform']}:{row['conversation_id']}",
            "count": int(row["n"]),
            "latest_seq": int(row["latest_seq"]),
            "latest_at": str(row["latest_at"] or ""),
        } for row in rows]

    def thread_messages_desc(self, thread: str,
                             limit: int = 100) -> list[dict[str, Any]]:
        """某会话最新的 N 条消息（seq 降序）。"""
        platform, conversation_id = _parse_thread(thread)
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS)
            + " FROM messages WHERE platform = ? AND conversation_id = ?"
            + " ORDER BY seq DESC LIMIT ?",
            (platform, conversation_id, int(limit)))
        return [message_to_dict(row) for row in rows]

    def latest_per_thread(self, limit: int = 5) -> list[dict[str, Any]]:
        """每个会话最新一条消息（无条件：任何 direction、任何已读状态，
        含自己发送的 out 消息），按最新 seq 降序取前 ``limit`` 个会话。
        主表 + 归档表合并。只读查询，不改已读状态。"""
        self._ensure_archive()
        columns = ", ".join(f"m.{col}" for col in MESSAGE_COLUMNS)
        rows = self._db.query(
            "SELECT " + columns + " FROM ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY platform,"
            " conversation_id ORDER BY seq DESC) AS rn"
            " FROM (SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages"
            " UNION ALL SELECT " + ", ".join(MESSAGE_COLUMNS)
            + " FROM messages_archive)) AS m"
            " WHERE m.rn = 1 ORDER BY m.seq DESC LIMIT ?",
            (int(limit),))
        return [message_to_dict(row) for row in rows]

    def unread_messages(self, thread: str,
                        limit: int = 20,
                        window_hours: float = 0) -> list[dict[str, Any]]:
        """某会话最新未读（in & sent_to_llm=0，seq 降序）。
        ``window_hours`` > 0 时只统计最近 N 小时内的未读。"""
        platform, conversation_id = _parse_thread(thread)
        clauses = ["platform = ?", "conversation_id = ?",
                   "direction = 'in'", "sent_to_llm = 0"]
        params: list[Any] = [platform, conversation_id]
        if window_hours > 0:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = (_dt.now() - _td(hours=window_hours)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            clauses.append("created_at >= ?")
            params.append(cutoff)
        params.append(int(limit))
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages WHERE "
            + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?",
            tuple(params))
        return [message_to_dict(row) for row in rows]

    def unread_count_by_thread(self, thread: str,
                               window_hours: float = 0) -> int:
        platform, conversation_id = _parse_thread(thread)
        clauses = ["platform = ?", "conversation_id = ?",
                   "direction = 'in'", "sent_to_llm = 0"]
        params: list[Any] = [platform, conversation_id]
        if window_hours > 0:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = (_dt.now() - _td(hours=window_hours)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            clauses.append("created_at >= ?")
            params.append(cutoff)
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE "
            + " AND ".join(clauses), tuple(params))
        return int(row["n"])

    def clear_unread_before(self, thread: str, before_time: str,
                             show: int = 0) -> tuple[int, list[dict[str, Any]]]:
        """Mark read every unread in-bound message of the conversation with
        ``created_at <= before_time`` (YYYY-MM-DD HH:MM:SS).

        Returns (cleared_count, cleared_messages). ``show`` > 0 时同时返回
        被清除消息中最新 N 条（seq 降序，供工具展示；不足返回实际条数）。"""
        platform, conversation_id = _parse_thread(thread)
        where = ("platform = ? AND conversation_id = ?"
                 " AND direction = 'in' AND sent_to_llm = 0"
                 " AND created_at <= ?")
        params = (platform, conversation_id, str(before_time))
        rows: list[Any] = []
        if show > 0:
            rows = self._db.query(
                "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages"
                " WHERE " + where + " ORDER BY seq DESC LIMIT ?",
                tuple(params) + (int(show),))
        self._db.execute(
            "UPDATE messages SET sent_to_llm = 1 WHERE " + where, params)
        self._db.commit()
        cleared = int(self._db.query_one("SELECT changes() AS n")["n"])
        return cleared, [message_to_dict(row) for row in rows]

    # ── range / keys / readonly query ──────────────────────────────────

    def messages_by_range(self, threads: Optional[list[str]] = None,
                          start_time: str = "", end_time: str = "",
                          include_archive: bool = False,
                          limit: int = 500) -> list[dict[str, Any]]:
        """会话级（threads）+ 时间区间查询；threads=None 表示 system:all。
        可选合并归档表。created_at 文本（YYYY-MM-DD HH:MM:SS）字典序即
        时间序。"""
        where: list[str] = []
        params: list[Any] = []
        if threads:
            for platform, conversation_id in [_parse_thread(t)
                                              for t in threads]:
                where.append("(platform = ? AND conversation_id = ?)")
                params.extend((platform, conversation_id))
        if start_time:
            where.append("created_at >= ?")
            params.append(str(start_time))
        if end_time:
            where.append("created_at <= ?")
            params.append(str(end_time))
        if not where:
            raise ValueError("threads/start_time/end_time 至少提供一个条件")
        self._ensure_archive()
        tables = "messages"
        if include_archive:
            tables = ("(SELECT " + ", ".join(MESSAGE_COLUMNS)
                      + " FROM messages UNION ALL SELECT "
                      + ", ".join(MESSAGE_COLUMNS) + " FROM messages_archive)")
        params.append(int(limit))
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM " + tables
            + " WHERE " + " AND ".join(where)
            + " ORDER BY seq LIMIT ?", tuple(params))
        return [message_to_dict(row) for row in rows]

    def messages_by_keys(self, conv_keys: list[str],
                         start_time: str = "", end_time: str = "",
                         include_archive: bool = False,
                         limit: int = 500) -> list[dict[str, Any]]:
        """按消息级 conversation_key 列表查询（不去重——撤回/notice 等
        同 key 多行全返回），可选合并归档表。"""
        where: list[str] = []
        params: list[Any] = []
        if conv_keys:
            keys = list(dict.fromkeys(str(k) for k in conv_keys if str(k)))
            if not keys:
                raise ValueError("conv_keys 为空")
            placeholders = ", ".join("?" for _ in keys)
            where.append(f"conversation_key IN ({placeholders})")
            params.extend(keys)
        if start_time:
            where.append("created_at >= ?")
            params.append(str(start_time))
        if end_time:
            where.append("created_at <= ?")
            params.append(str(end_time))
        if not where:
            raise ValueError("conv_keys/start_time/end_time 至少提供一个条件")
        self._ensure_archive()
        tables = "messages"
        if include_archive:
            tables = ("(SELECT " + ", ".join(MESSAGE_COLUMNS)
                      + " FROM messages UNION ALL SELECT "
                      + ", ".join(MESSAGE_COLUMNS) + " FROM messages_archive)")
        params.append(int(limit))
        rows = self._db.query(
            "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM " + tables
            + " WHERE " + " AND ".join(where)
            + " ORDER BY seq LIMIT ?", tuple(params))
        return [message_to_dict(row) for row in rows]

    def run_readonly_query(self, sql: str,
                           max_rows: int = 200) -> list[dict[str, Any]]:
        """Agent SQL query tool: SELECT-only, single statement, no
        side-effecting keywords, FROM restricted to the message tables
        (defense against injection / DDL / cross-table reads)."""
        statement = str(sql or "").strip().rstrip(";").strip()
        lowered = statement.lower()
        if not lowered.startswith("select"):
            raise ValueError("只允许 SELECT 查询语句")
        if ";" in statement:
            raise ValueError("只允许单条语句（不支持分号分隔）")
        for keyword in ("insert", "update", "delete", "drop", "alter",
                        "create", "attach", "pragma", "vacuum", "begin",
                        "commit", "rollback", "replace", "grant"):
            # 词边界匹配：created_at / replacement / deleted 等标识符里的
            # 子串不能算关键字（否则正常查询 created_at 会被误拒）
            if re.search(rf"\b{keyword}\b", lowered):
                raise ValueError(f"不允许的关键字: {keyword!r}（只读查询）")
        # FROM 主表白名单：只能查 messages / messages_archive
        # （或工具自身生成的联合子查询 "FROM (SELECT ..."）
        from_match = re.search(r"\bfrom\s+([a-zA-Z_][a-zA-Z0-9_]*|\()",
                               lowered)
        if from_match is not None:
            table = from_match.group(1)
            if table == "(":
                pass  # 联合子查询（工具自身生成，内部仍为两消息表）
            elif table not in ("messages", "messages_archive"):
                raise ValueError(
                    f"只允许查询 messages / messages_archive 表，收到: "
                    f"{table!r}")
        if "limit" not in lowered:
            statement += f" LIMIT {int(max_rows)}"
        rows = self._db.query(statement)
        return [dict(row) for row in rows]

    # ── archive ────────────────────────────────────────────────────────

    def _ensure_archive(self) -> None:
        """messages_archive: same-shape table (column order matches messages
        so UNION/INSERT-by-name stay aligned)."""
        columns = {key: MESSAGES_SCHEMA[key] for key in MESSAGE_COLUMNS}
        self._db.create_table("messages_archive", columns)
        self._db.commit()

    def archive_expired(self, days: int) -> int:
        """Move consumed (sent_to_llm=1), older-than-N-days messages into
        messages_archive; returns the number moved. Unread never archived."""
        self._ensure_archive()
        days = max(1, int(days))
        cutoff = (datetime.now().replace(microsecond=0)
                  - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        columns = ", ".join(MESSAGE_COLUMNS)
        moved = 0
        while True:
            rows = self._db.query(
                f"SELECT {columns} FROM messages WHERE created_at < ?"
                " AND sent_to_llm = 1 ORDER BY seq LIMIT 200",
                (cutoff,))
            if not rows:
                break
            for row in rows:
                values = tuple(row[col] for col in MESSAGE_COLUMNS)
                self._db.execute(
                    f"INSERT INTO messages_archive ({columns})"
                    f" VALUES ({', '.join('?' * len(MESSAGE_COLUMNS))})",
                    values)
                self._db.execute(
                    "DELETE FROM messages WHERE seq = ?", (row["seq"],))
            self._db.commit()
            moved += len(rows)
        if moved:
            logger.info("archived %d messages (days=%d cutoff=%s)",
                        moved, days, cutoff)
        return moved

    def archive_query(self, platform: str = "", conversation_id: str = "",
                      before: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Query the archive table (slow path)."""
        self._ensure_archive()
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if before:
            clauses.append("created_at < ?")
            params.append(before)
        sql = "SELECT " + ", ".join(MESSAGE_COLUMNS) + " FROM messages_archive"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        rows = self._db.query(sql, tuple(params))
        return [message_to_dict(row) for row in rows]


def _parse_thread(thread: str) -> tuple[str, str]:
    """Parse ``platform:conversation_id`` (session identity)."""
    text = str(thread or "").strip()
    parts = text.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"thread 格式应为 platform:conversation_id，收到: {text!r}")
    return parts[0].strip(), parts[1].strip()