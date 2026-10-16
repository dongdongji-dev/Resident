"""Crontab-style schedule matching for recurring notices.

Expression: ``f1 f2 f3 f4 f5``

- f1 minute (0-59), f2 hour (0-23), f3 day of month (1-31),
  f4 month (1-12), f5 weekday (0-6, Sunday=0)
- ``*`` every unit; ``a-b`` range; ``*/n`` every n units; ``a,b,c`` list
"""

from __future__ import annotations

from datetime import datetime


def _field_match(field: str, value: int) -> bool:
    field = field.strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            n = int(field[2:])
            return n > 0 and value % n == 0
        except ValueError:
            return False
    if "-" in field and not field.startswith("*"):
        lo, _, hi = field.partition("-")
        try:
            return int(lo) <= value <= int(hi)
        except ValueError:
            return False
    if "," in field:
        return value in (int(p) for p in field.split(",") if p.strip().isdigit())
    try:
        return int(field) == value
    except ValueError:
        return False


def cron_match(expr: str, dt: datetime) -> bool:
    """True when ``expr`` (crontab) matches the given datetime's minute."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # crontab weekday: 0=Sunday; python weekday(): 0=Monday
    py_dow = dt.weekday()
    cron_dow = (py_dow + 1) % 7
    return (
        _field_match(minute, dt.minute)
        and _field_match(hour, dt.hour)
        and _field_match(dom, dt.day)
        and _field_match(month, dt.month)
        and _field_match(dow, cron_dow)
    )


def _field_valid(field: str) -> bool:
    """Syntactic validation of one crontab field."""
    field = field.strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            return int(field[2:]) > 0
        except ValueError:
            return False
    if field.startswith("*"):
        return False
    if "-" in field:
        lo, _, hi = field.partition("-")
        try:
            return int(lo) >= 0 and int(lo) <= int(hi)
        except ValueError:
            return False
    if "," in field:
        parts = [p for p in field.split(",")]
        return bool(parts) and all(p.strip().isdigit() for p in parts)
    return field.isdigit()


def is_valid_cron(expr: str) -> bool:
    """Validate a crontab expression (``分 时 日 月 周``, 5 fields)."""
    fields = str(expr or "").strip().split()
    if len(fields) != 5:
        return False
    return all(_field_valid(f) for f in fields)