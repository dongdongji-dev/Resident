"""Agent-facing focus-mode tool: open / renew / close the DND window —
``all`` (full suppression + periodic unread-count reminders) or session
filters (``whitelist`` / ``blacklist``; only passing sessions may wake,
filtered-out messages are condensed to counts, no periodic reminders).

Aligned with the ``GET /api/focus`` / ``POST /api/focus/release`` HTTP
endpoints; the Beat drives the wake suppression and the reminders."""

from __future__ import annotations

from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger
from kernel.osdb.messages import MessagesStore
from kernel.osdb.system_vars import SystemVarsStore
from kernel.scheduler.focus import (FocusConfig, cancel, is_active,
                                    normalize_session, request_flush, start)

logger = get_logger("kernel.agent_framework.focus_tools")


def make_focus_mode(system_vars: SystemVarsStore,
                    focus_cfg: FocusConfig,
                    messages: Optional[MessagesStore] = None) -> Any:
    def focus_mode(minutes: int, mode: str = "all",
                   sessions: Optional[list[str]] = None) -> dict[str, Any]:
        """开启 / 续期 / 退出专注模式（支持白/黑名单会话筛选）。

        参数:
          minutes: 专注分钟数。大于 0 时把「专注模式剩余时间」重置为
            minutes 分钟后过期（超上限按配置截断，默认 120 分钟）；
            小于等于 0 时立即退出专注模式（积压消息恢复推送）。
          mode: 专注模式类型（默认 all）——
            all：全屏蔽。期间一切外部消息（含紧急唤醒）都不主动推送，
              只按间隔推送未读统计提醒；
            whitelist：白名单。仅 sessions 列表内的会话可触发推送，
              其余会话的消息只以未读条数浓缩上报（不展示内容）；
            blacklist：黑名单。sessions 列表内的会话消息只以未读条数
              浓缩上报，其余会话正常推送。
            白名单/黑名单模式没有周期提醒，到期后自动恢复正常。
          sessions: 会话标识列表（白/黑名单模式必填），如
            ["qq:group_200000001", "qq:private_10001"]；多余的第三段
            会被忽略（传完整消息 key 也可以）。工具会校验会话是否在
            消息库出现过：全部未命中会拒绝开启并给出库中可用示例，
            部分未命中会附警告（可能拼写有误）。

        专注模式效果: 被屏蔽/筛除的消息不会主动推送、不展开内容，但都会
        正常写入消息库、不会丢失；到期或退出后积压消息会立即恢复推送。

        返回: 当前专注状态、模式、名单、到期时间等。
        """
        minutes = int(minutes)
        if minutes <= 0:
            was_active = is_active(system_vars)
            cancel(system_vars)
            if was_active:
                request_flush(system_vars)  # beat 下一 tick 强制带回积压
            logger.info("focus cancelled by tool (was_active=%s)", was_active)
            return {"focus": False,
                    "message": "已退出专注模式，外部消息恢复推送。"}

        mode = str(mode or "all").strip().lower()
        if mode not in ("all", "whitelist", "blacklist"):
            raise ValueError(
                f"mode 只能是 all / whitelist / blacklist，收到: {mode!r}")
        chosen: list[str] = []
        missing: list[str] = []
        if mode != "all":
            seen: set[str] = set()
            for item in (sessions or []):
                session = normalize_session(str(item))
                if session and session not in seen:
                    seen.add(session)
                    chosen.append(session)
            if not chosen:
                raise ValueError(
                    "whitelist/blacklist 模式需要提供非空的 sessions 列表"
                    "（会话标识如 qq:group_123）")
            if messages is not None:
                missing = [s for s in chosen
                           if not messages.session_exists(s)]
                if missing and len(missing) == len(chosen):
                    known = messages.recent_sessions(8)
                    hint = ("、".join(known) if known
                            else "（消息库暂无可参考的会话）")
                    example = f"，例如 {known[0]}" if known else ""
                    raise ValueError(
                        "白名单/黑名单会话在消息库中没有任何记录，"
                        "可能是会话标识写错"
                        f"（未命中：{'、'.join(missing)}）。"
                        f"库中最近活跃的会话：{hint}；格式为 "
                        f"platform:conversation_id{example}")

        effective, until = start(system_vars, minutes, focus_cfg,
                                 mode=mode, sessions=tuple(chosen))
        if mode == "all":
            message = (f"已开启专注模式（全屏蔽）：{effective} 分钟"
                       f"（至 {until} 到期）")
        else:
            kind = "白名单" if mode == "whitelist" else "黑名单"
            message = (f"已开启专注筛选模式（{kind}）：{effective} 分钟"
                       f"（至 {until} 到期）；名单：{'、'.join(chosen)}")
        if effective < minutes:
            message += (f"；请求的 {minutes} 分钟超过上限 "
                        f"{focus_cfg.max_minutes} 分钟，已按上限设置")
        if mode == "all":
            message += (f"。期间外部消息不会主动推送（每 "
                        f"{focus_cfg.reminder_minutes} 分钟提醒一次未读统计）；"
                        "调用本工具传 0 可提前退出。")
        else:
            message += ("。被筛除的消息只以未读条数汇报、不展开内容；"
                        "无周期提醒；调用本工具传 0 可提前退出。")
        if missing:
            message += (f" ⚠️ 注意：{'、'.join(missing)} 未在消息库中"
                        "出现过，可能拼写有误，请核对后重新设置。")
        logger.info("focus started minutes=%d effective=%d mode=%s "
                    "sessions=%d until=%s",
                    minutes, effective, mode, len(chosen), until)
        return {"focus": True, "minutes": effective, "mode": mode,
                "sessions": chosen, "until": until,
                "max_minutes": focus_cfg.max_minutes,
                "reminder_minutes": focus_cfg.reminder_minutes,
                "message": message}
    return focus_mode
