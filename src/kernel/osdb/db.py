
"""Thread-safe sqlite3 foundation for os.db (standard library only)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.db")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._closed = False

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.path), timeout=5, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._closed = False
            logger.debug("os.db opened: %s", self.path)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._closed = True
            logger.debug("os.db closed: %s", self.path)

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not open")
        return self._connection

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._require().execute(sql, params)

    def execute_returning(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT and return the last row id (for AUTOINCREMENT PKs)."""
        with self._lock:
            cursor = self._require().execute(sql, params)
            last = cursor.lastrowid
            cursor.close()
            return int(last)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._require().execute(sql, params)
            rows = cursor.fetchall()
            cursor.close()
            return rows

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        with self._lock:
            self._require().commit()

    def create_table(self, name: str, columns: dict[str, str]) -> None:
        definition = ", ".join(f"{col} {ddl}" for col, ddl in columns.items())
        with self._lock:
            self._require().execute(
                f"CREATE TABLE IF NOT EXISTS {name} ({definition})")
            self._require().commit()
            logger.debug("table ensured: %s", name)

