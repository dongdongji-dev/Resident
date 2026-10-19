"""Focus mode（专注模式）: bounded, self-expiring do-not-disturb state.

Bounded by config (``focus.max_minutes``, default 120) — the Agent opens /
renews / closes the window via the ``focus_mode`` tool; the console exposes
status and a manual release. Two kinds of windows: ``all`` (suppress
every message wake; periodic count reminders) and session filters
(``whitelist`` / ``blacklist`` — only listed / non-listed sessions may
wake; filtered-out messages are condensed to unread counts, no
periodic reminders). State lives in ``system_vars`` (os.db is the
single source of truth):

- ``focus_until``          — expiry ``YYYY-MM-DD HH:MM:SS``; absent = off;
- ``focus_started_at``     — start of the current stretch (kept across
                             renewals, so humans can see total focus time);
- ``focus_next_reminder``  — next reminder notice due time;
- ``focus_mode``          — ``all`` / ``whitelist`` / ``blacklist``;
- ``focus_sessions``      — JSON list of session keys for the filter;
- ``focus_resume_flush``  — set on explicit cancel/release so the next
                             Beat tick force-fires one wake (backlog).

While focus is active the Beat suppresses ALL message-driven wakes (urgent
included) and only pushes a periodic reminder notice carrying per-session
unread COUNTS (never content). When the window expires the Beat clears the
state and force-fires one wake so the backlog is delivered immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from kernel.agent_framework.timeutil import fmt, now_plus, now_str, parse
from kernel.osdb.system_vars import SystemVarsStore

KEY_UNTIL = "focus_until"
KEY_STARTED = "focus_started_at"
KEY_NEXT_REMINDER = "focus_next_reminder"
KEY_MODE = "focus_mode"
KEY_SESSIONS = "focus_sessions"
KEY_RESUME = "focus_resume_flush"

FOCUS_MODES = ("all", "whitelist", "blacklist")


@dataclass(frozen=True, slots=True)
class FocusConfig:
    max_minutes: int = 120
    reminder_minutes: int = 3


def load_focus_config(config: dict) -> FocusConfig:
    """Read the optional ``focus`` YAML section; missing keys use defaults."""
    raw = config.get("focus") or {}
    return FocusConfig(
        max_minutes=max(1, int(raw.get("max_minutes", 120))),
        reminder_minutes=max(1, int(raw.get("reminder_minutes", 3))),
    )


def normalize_session(value: str) -> str:
    """Normalize a conversation key to a session (``platform:conv``):
    extra segments are dropped, whitespace trimmed."""
    parts = str(value).strip().split(":", 2)
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}:{parts[1]}"
    return str(value).strip()


def focus_until(system_vars: SystemVarsStore) -> Optional[datetime]:
    raw = system_vars.get(KEY_UNTIL)
    if not raw:
        return None
    until = parse(raw)
    if until is None:
        # 坏数据按「未开启」处理并清干净，避免每 tick 重复解析失败
        cancel(system_vars)
        return None
    return until


def is_active(system_vars: SystemVarsStore,
              now: Optional[datetime] = None) -> bool:
    until = focus_until(system_vars)
    return until is not None and (now or datetime.now()) < until


def start(system_vars: SystemVarsStore, minutes: int, cfg: FocusConfig,
          mode: str = "all",
          sessions: tuple[str, ...] = ()) -> tuple[int, str]:
    """Open or renew the window: remaining time is reset to ``minutes``
    (clamped to ``cfg.max_minutes``); returns (effective_minutes, until).

    ``mode`` selects the window kind (``all`` / ``whitelist`` /
    ``blacklist``); ``sessions`` is the filter list (already
    normalized). ``focus_started_at`` and the reminder schedule are only
    (re)initialized when the window is not already active — frequent
    renewals must not starve the reminders nor hide how long focus has
    lasted.
    """
    effective = min(int(minutes), cfg.max_minutes)
    now = datetime.now()
    until = now_plus(effective * 60)
    if not is_active(system_vars, now):
        cancel(system_vars)  # 清掉可能的过期残留
        system_vars.set(KEY_STARTED, now_str())
        system_vars.set(KEY_NEXT_REMINDER,
                        now_plus(cfg.reminder_minutes * 60))
    system_vars.set(KEY_MODE, mode if mode in FOCUS_MODES else "all")
    system_vars.set(KEY_SESSIONS,
                    json.dumps(list(sessions), ensure_ascii=False))
    system_vars.set(KEY_UNTIL, until)
    return effective, until


def cancel(system_vars: SystemVarsStore) -> None:
    for key in (KEY_UNTIL, KEY_STARTED, KEY_NEXT_REMINDER,
                KEY_MODE, KEY_SESSIONS, KEY_RESUME):
        system_vars.clear(key)


def request_flush(system_vars: SystemVarsStore) -> None:
    """Explicit cancel/release: ask the Beat to force one wake on the
    next tick so the backlog is delivered immediately (same effect as
    natural expiry)."""
    system_vars.set(KEY_RESUME, "1")


def filter_context(system_vars: SystemVarsStore) -> Optional[dict]:
    """Active session filter: {"mode": "whitelist"|"blacklist",
    "sessions": set(...)}; ``None`` when mode is ``all`` or the list is
    empty (degenerate filter falls back to full suppression)."""
    mode = system_vars.get(KEY_MODE) or "all"
    if mode not in ("whitelist", "blacklist"):
        return None
    raw = system_vars.get(KEY_SESSIONS)
    try:
        sessions = {str(s) for s in json.loads(raw)} if raw else set()
    except (TypeError, ValueError):
        sessions = set()
    if not sessions:
        return None
    return {"mode": mode, "sessions": sessions}


def reminder_due(system_vars: SystemVarsStore,
                 now: Optional[datetime] = None) -> bool:
    raw = system_vars.get(KEY_NEXT_REMINDER)
    due = parse(raw) if raw else None
    return due is not None and (now or datetime.now()) >= due


def bump_reminder(system_vars: SystemVarsStore, cfg: FocusConfig,
                  now: Optional[datetime] = None) -> None:
    """Schedule the next reminder ``reminder_minutes`` after ``now``."""
    base = fmt(now) if now is not None else None
    system_vars.set(KEY_NEXT_REMINDER,
                    now_plus(cfg.reminder_minutes * 60, base=base))


def status(system_vars: SystemVarsStore) -> dict:
    """Snapshot for the console: active flag, mode, list, expiry."""
    raw_sessions = system_vars.get(KEY_SESSIONS)
    try:
        sessions = (sorted(str(s) for s in json.loads(raw_sessions))
                    if raw_sessions else [])
    except (TypeError, ValueError):
        sessions = []
    mode = system_vars.get(KEY_MODE) or "all"
    until = focus_until(system_vars)
    if until is None:
        return {"active": False, "until": None, "started_at": None,
                "remaining_seconds": 0, "remaining_minutes": 0,
                "mode": None, "sessions": []}
    remaining = int((until - datetime.now()).total_seconds())
    return {"active": remaining > 0, "until": fmt(until),
            "started_at": system_vars.get(KEY_STARTED),
            "remaining_seconds": max(0, remaining),
            "remaining_minutes": max(0, remaining // 60),
            "mode": mode, "sessions": sessions}


def reminder_text(*, now_label: str, until_label: str,
                  remaining_minutes: int,
                  started_at: Optional[datetime], total: int,
                  sessions: dict[str, int], cfg: FocusConfig,
                  session_limit: int = 8) -> str:
    """Compose the periodic focus reminder (counts only — never content)."""
    elapsed_note = ""
    if started_at is not None:
        elapsed = max(
            0, int((datetime.now() - started_at).total_seconds() // 60))
        elapsed_note = f"，本次已专注约 {elapsed} 分钟"
    lines = [
        f"当前时间：{now_label}",
        f"你正处于专注模式：将于 {until_label} 到期"
        f"（剩余约 {remaining_minutes} 分钟{elapsed_note}）。",
        "专注模式效果：外部消息不会主动推送给你（含紧急唤醒），"
        "但消息仍会正常写入消息库、不会丢失；"
        f"每 {cfg.reminder_minutes} 分钟通过本提醒汇报一次未读统计。",
    ]
    if total:
        listed = sorted(sessions.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = "；".join(f"{name} {count} 条"
                          for name, count in listed[:session_limit])
        if len(listed) > session_limit:
            shown += f"；另有 {len(listed) - session_limit} 个会话"
        lines.append(f"当前未读消息：共 {total} 条（{shown}）。")
    else:
        lines.append("当前没有未读消息。")
    lines.append("请先完成手上要紧的工作，再退出专注模式回复消息"
                 "（调用 focus_mode 工具传 0 立即退出；"
                 "管理员也可以在控制台手动解除）。")
    return "\n".join(lines)
