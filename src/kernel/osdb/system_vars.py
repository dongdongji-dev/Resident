
"""Persistent system variables — one table in os.db.

Schema: ``key / value / lastupdate``. os.db is the single source of truth;
there is deliberately no in-memory shadow copy.
"""

from __future__ import annotations

from typing import Any, Optional

from kernel.agent_framework.timeutil import now_plus, now_str as _now

_SYSTEM_VARS_SCHEMA = {
    "key": "TEXT PRIMARY KEY",
    "value": "TEXT NOT NULL",
    "lastupdate": "TEXT NOT NULL",
}


def ensure_system_vars_table(db: Any) -> None:
    db.create_table("system_vars", _SYSTEM_VARS_SCHEMA)
    db.commit()


class SystemVarsStore:
    def __init__(self, db: Any) -> None:
        self._db = db
        ensure_system_vars_table(db)

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO system_vars (key, value, lastupdate) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "lastupdate = excluded.lastupdate",
            (key, value, _now()))
        self._db.commit()

    def get(self, key: str) -> Optional[str]:
        row = self._db.query_one(
            "SELECT value FROM system_vars WHERE key = ?", (key,))
        return row["value"] if row else None

    def get_row(self, key: str) -> Optional[dict[str, str]]:
        row = self._db.query_one(
            "SELECT key, value, lastupdate FROM system_vars WHERE key = ?",
            (key,))
        return (dict(row) if row else None)

    def clear(self, key: str) -> None:
        self._db.execute("DELETE FROM system_vars WHERE key = ?", (key,))
        self._db.commit()

    def all(self) -> list[dict[str, str]]:
        return [dict(row) for row in self._db.query(
            "SELECT key, value, lastupdate FROM system_vars ORDER BY key")]



