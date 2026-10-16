"""Project-wide time rules.

All timestamps use the SYSTEM TIMEZONE, local format ``YYYY-MM-DD HH:MM:SS``
— no ``Z``, no UTC marker, no ISO offset. Lexicographic order equals
chronological order, so string comparison works everywhere.

Every module (events, system_vars, tools, db) must go through these
helpers instead of inventing its own timestamp format.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def now_str() -> str:
    """Current local time ``YYYY-MM-DD HH:MM:SS`` in the system timezone."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_plus(seconds: int, base: Optional[str] = None) -> str:
    """Local-time string for ``base`` + seconds; ``base`` defaults to now.
    ``base`` must already be in ``YYYY-MM-DD HH:MM:SS`` (system timezone)."""
    instant = parse(base) if base is not None else datetime.now()
    return fmt(datetime.fromtimestamp(
        instant.timestamp() + int(seconds)))


def fmt(value: datetime) -> str:
    """Format a datetime as ``YYYY-MM-DD HH:MM:SS`` (local, no tz marker)."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse(value: str) -> Optional[datetime]:
    """Parse ``YYYY-MM-DD HH:MM:SS`` back to a naive local datetime; None
    when malformed."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
