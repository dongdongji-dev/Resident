"""运行时消息标识块 —— 系统提示词里按部署配置动态生成的一段说明。

背景：agent 在上下文里会看到各种系统注入的标识（消息信封、唤醒引导语、
通知信封、后台完成、自说自话、重启、静默标记、读图标签……）。这些标识
的格式与文案一部分来自 config/agent.yaml（前缀、开关、路径），一部分是
代码里的固定常量。**本模块不手写死描述**：

- 可配置项：一律从传入的 config 原样读取（配置改了、重启后注入即变）；
- 代码常量：引用运行时同一个源（beat/sandbox/image_tools/protocol/reboot
  里的共享常量），消息信封示例直接调用真实的 ``_msg_xml`` 渲染，
  保证「实际推送长什么样，系统提示词就写什么样」；
- 功能开关（free_preview / inject_latest / readimage / workspace_tools）：
  关闭时对应内容不注入或改写为「未开启」，避免描述与行为不一致。
"""

from __future__ import annotations

from typing import Any

from kernel.agent_framework.image_tools import READIMAGE_LABEL_TEMPLATE
from kernel.agent_framework.protocol import DEFAULT_SILENCE_MARKER
from kernel.agent_framework.reboot import REBOOT_NOTICE_MESSAGE
from kernel.agent_framework.sandbox import BACKGROUND_DONE_PREFIX
from kernel.scheduler.beat import (NOT_CARRIED_HINT, NOTICE_ENVELOPE,
                                   PREVIEW_HEADER, TAIL_LINE_TEMPLATE,
                                   URGENT_PREFIX, _msg_xml)

_SAMPLE_MESSAGE: dict[str, Any] = {
    "conversation_key": "平台:会话ID:消息ID",
    "conversation_type": "群聊",
    "created_at": "2026-01-01 00:00:00",
    "seq": 12345,
    "content": "消息正文内容",
    "sender_data": {"id": "发送者ID", "name": "发送者昵称"},
}


def build_runtime_block(config: dict[str, Any]) -> str:
    """按当前配置渲染「运行时消息标识」块（无隐藏默认值：可配置项全部
    取自 config；可选功能以 enabled 字段为准）。"""
    wake = config["wake"]
    readimage = config.get("readimage") or {}
    workspace_tools = config.get("workspace_tools") or {}

    parts: list[str] = [
        "## 运行时消息标识（由系统按当前部署配置自动生成）",
        "以下标识由系统在运行时自动加入你收到的消息，用于标明来源与机制；"
        "它们不是你正在对话的对象。",
    ]

    # ── 消息信封 ──────────────────────────────────────────────────────
    sample = _msg_xml(dict(_SAMPLE_MESSAGE), 1000)
    parts.append(
        "### 外部消息（消息信封）\n\n"
        "外部平台的消息以如下格式进入上下文（示例）：\n\n"
        f"{sample}\n\n"
        "- conversation_key：三段式「平台:会话ID:消息ID」，前两段是会话"
        "身份（同一会话的消息前两段相同），第三段标识单条消息；\n"
        "- conversation_type：会话类型（群聊/私聊等，取值由接入适配器定义）；\n"
        "- msg_time：消息发送时间；seq：消息在系统中的序号；\n"
        "- sender_data：发送者数据（JSON，字段由接入适配器定义）；\n"
        "- `<msg>` 标签内的正文即消息内容；\n"
        f"- 行首「{URGENT_PREFIX}」：该消息是本会话新到、刚推给你的消息，"
        "通常需要优先关注。")

    # ── 唤醒（引导语原样引用配置值）+ 主动唤醒 ─────────────────────────
    parts.append(
        "### 唤醒\n\n"
        "- 有新消息 / 有到期通知 / 空闲自主时间时，系统会唤醒你；唤醒以一条"
        "普通「用户」消息进入上下文，开头带引导语（当前配置原文）：\n\n"
        f"> {wake['wake_notice_prefix']}\n\n"
        "- 若该会话已有消息在等待队列里（上一轮唤醒尚未处理完），新到的"
        f"「{URGENT_PREFIX}」消息不再重复完整开头，只经补充通道推送，"
        "开头为（当前配置原文）：\n\n"
        f"> {wake['wake_supplement_prefix']}\n\n"
        "- 没有任何新消息的主动唤醒以「当前时间：…」开头，正文来自提示文件 "
        f"`{wake['no_message_prompt_file']}`——那是给你的自主活动窗口，"
        "不是新消息。")

    # ── 推送机制（数量尾注含义 + 各开关按配置） ───────────────────────
    if wake["inject_latest"]:
        carried = ("「最早一条 + 本会话绝对最新一条（含你自己发出的消息）」")
    else:
        carried = "「最早一条」"
    tail_example = TAIL_LINE_TEMPLATE.format(displayed=3, remaining=12)
    preview_on = bool(wake["free_preview_enabled"])
    if preview_on:
        preview_line = (
            f"- 自由活动预览（已开启，最多 {int(wake['free_preview_sessions'])} "
            "个会话）：没有新消息的唤醒会附带"
            f"「{PREVIEW_HEADER}」及若干消息块，仅作了解、不需要回复、"
            "不标记已读。")
    else:
        preview_line = "- 自由活动预览：本部署未开启。"
    parts.append(
        "### 消息推送机制\n\n"
        "- 每次唤醒不会把所有未读全部推给你：每个会话最多推送"
        f"{carried}，另外附上本批新到的「{URGENT_PREFIX}」消息；\n"
        f"- 会话推送后会带一行统计，例如「{tail_example}」——"
        "未被展示的消息仍留在系统里（可随时回查，也会在后续唤醒中继续提示）；\n"
        "- 展示过的消息即记为已读；需要细节时用 messages_* 工具按会话回查；\n"
        f"- 单次唤醒最多携带 {int(wake['wake_x_convs'])} 个会话，"
        f"超出的会话只提示「{NOT_CARRIED_HINT}」；\n"
        f"{preview_line}")

    # ── 系统通知（信封 + 来源枚举，开关裁剪） ─────────────────────────
    notice_example = NOTICE_ENVELOPE.format(notice_id="编号", time="时间")
    sources = ["- `notice_create` 到期推送（提醒、定时任务）；"]
    if workspace_tools.get("enabled"):
        sources.append(
            "- 后台任务完成（`workspace_exec_async`）："
            f"`{BACKGROUND_DONE_PREFIX}task_id]` 命令/状态/耗时/输出摘要；")
    sources.append(
        "- 自说自话检测：系统发现你输出了未对外发送的文字会提醒你确认"
        "是否有遗漏的发送动作——那些内容没有送达任何人；")
    sources.append("- 唤醒与补充通知（格式见上）。")
    parts.append(
        "### 系统通知\n\n"
        "通知以如下格式进入上下文：\n\n"
        f"{notice_example}\n"
        "正文\n\n"
        "常见来源：\n"
        + "\n".join(sources))

    # ── 其他系统消息（开关裁剪） ──────────────────────────────────────
    others = [
        f"- 重启完成：「{REBOOT_NOTICE_MESSAGE}」——表示运行环境重载完成，"
        "继续原有工作即可，不是新的指令；",
        "- 静默标记：没有需要对外说的话时，单独一行输出 "
        f"`{str((config.get('agent') or {}).get('silence_marker') or DEFAULT_SILENCE_MARKER)}`；",
    ]
    if readimage.get("enabled"):
        label_example = READIMAGE_LABEL_TEMPLATE.format(label="图片路径")
        others.append(
            f"- 读取图片：`readimage` 入队后以「{label_example}」文字 + "
            "图片块出现（local 模式为内联图片数据，imgbed 模式为图片链接）。")
    parts.append("### 其他系统消息\n\n" + "\n".join(others))
    return "\n\n".join(parts)
