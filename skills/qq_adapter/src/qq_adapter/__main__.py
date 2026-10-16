"""QQ adapter entry point (Resident Kernel architecture).

Standalone background service, one instance owns one bot connection:

- inbound:  OneBot11 WS events -> MessageDraft -> POST core /api/messages
- outbound: local HTTP endpoint (POST /hook) that the agent calls (curl)
            to execute any of the 52 actions (send / query / manage /
            approve / delete) -> OneBot11 API over the WS connection
- no hook registration, no /api/stream usage (that's the management
  console's channel)

Run::

    NAPCAT_TOKEN=xxx python -m qq_adapter --config config/napcat.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from qq_adapter.logging_utils import configure_logging

from .actions import ActionExecutor
from .attention import AttentionStore
from .callback import CallbackServer
from .config import load_config
from .connection import OneBotClient
from .events import EventContext, dispatch_event
from .message import MessageSubmitter
from .whitelist import Whitelist
from . import handlers  # noqa: F401,E402  (注册全部事件分支)


class _Runtime:
    """adapter 运行时状态容器（on_connected 回调闭包引用 executor）。"""

    def __init__(self) -> None:
        self.executor: ActionExecutor | None = None

    async def on_connected(self, client: OneBotClient, config,
                          ctx: EventContext,
                          submitter: MessageSubmitter) -> None:
        """连接成功后用 get_login_info 更新平台实例标识
        （platform 占位 qq[10001] → qq[实际 bot QQ]）。
        connection.py 在事件循环内 await 本回调。"""
        try:
            info = await client.get_login_info()
            self_id = str(info.get("user_id", ""))
            nickname = str(info.get("nickname", ""))
            if self_id:
                platform = f"qq[{self_id}]"
                ctx.self_id = self_id
                ctx.platform = platform
                ctx.names[self_id] = nickname
                submitter.update_platform(platform)
                if self.executor is not None:
                    self.executor.set_platform(platform)
                    self.executor.set_self_identity(self_id, nickname)
                logging.getLogger("qq_adapter").info(
                    "login info: platform=%s nickname=%s", platform, nickname)
        except Exception:  # noqa: BLE001
            logging.getLogger("qq_adapter").exception(
                "login info fetch failed; keeping config platform=%s",
                config.platform)


# 保持与老项目一致：on_connected 以 client 为参数
def _make_on_connected(runtime: _Runtime, config, ctx, submitter):
    async def on_connected(client: OneBotClient) -> None:
        await runtime.on_connected(client, config, ctx, submitter)
    return on_connected


def _is_anomalous_event(event: dict) -> bool:
    """message 事件但没有可用文本内容（2026-09-06 排障：Napcat 把
    "对方已下载文件"通知当 message 推过来、segments/raw_message 全空，
    入库后成了空消息）。纯表情/纯语音也会命中（raw_message 空），
    量小可接受——落盘只为排障，不影响正常流程。"""
    if event.get("post_type") != "message":
        return False
    segments = event.get("message") or []
    raw = str(event.get("raw_message") or "").strip()
    return (not segments) or (not raw)


def _dump_raw_event(path: str, event: dict) -> None:
    """原始事件 JSON 单行追加（on_event 单线程事件循环内调用，无并发）。"""
    try:
        from datetime import datetime
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "event": event}, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter").exception("raw event dump failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ OneBot11 adapter")
    parser.add_argument("--config", action="append",
                        default=["config/default.yaml", "config/local.yaml"],
                        help="可重复：后一个覆盖前一个（通用默认 + 本地覆盖）")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(args.log_level, "text")
    state_dir = config.state_dir
    os.makedirs(state_dir, exist_ok=True)

    whitelist = Whitelist(config.whitelist)
    submitter = MessageSubmitter(config.core_url, config.core_api_key,
                                 config.platform)

    def notify_attention(reason: str, previous: str, current: str) -> None:
        """注意力状态转移 → 给自己推一条通知（2026-10-12 设计约定）。
        ⚠️ 走 core **notice 通道**（POST /api/notice，立即触发的 once），
        不落 messages 库——这是给"我"的自我提醒，不是一条消息。
        内容含：哪个会话、什么原因、影响、怎么开/关/查。"""
        from .attention import render_attention_notice
        text = render_attention_notice(reason, previous, current)
        if current and not previous:
            short = f"[注意力] 已开启 {current}：该群新消息全部唤醒我（无需 @）"
        elif current:
            short = f"[注意力] {previous} → {current}：改为此群新消息全部唤醒我"
        else:
            short = (f"[注意力] 已关闭（原 {previous or '无'}）："
                     "回到仅私聊 / @我 / 回复我才唤醒")
        submitter.create_notice(text, notification=short)

    attention = AttentionStore(  # 注意力群（运行时状态，state_dir 下）
        path=os.path.join(state_dir, "attention.json"),
        notifier=notify_attention)
    ctx = EventContext(submitter=submitter, self_id="",
                       platform=config.platform,
                       config_wakeup_on_mention=config.wakeup_on_mention,
                       media_download_timeouts=config.media_download_timeouts,
                       media_download_max_mb=config.media_download_max_mb,
                       attention=attention)
    runtime = _Runtime()

    async def on_event(event: dict) -> None:
        # input 侧白名单：非白名单会话的事件直接丢弃（不入库）
        if not whitelist.allow_inbound(event):
            return
        # 原始事件落盘（排障：抓空内容事件的真实 payload）
        if config.raw_events.enabled and (
                config.raw_events.mode == "all"
                or _is_anomalous_event(event)):
            _dump_raw_event(config.raw_events.path, event)
        await dispatch_event(event, ctx)

    client = OneBotClient(config, on_event,
                          on_connected=_make_on_connected(
                              runtime, config, ctx, submitter))

    # 大文件发送：adapter 文件服务（napcat 按 URL 拉取，跨服务器可用）
    media_server = None
    serve_url = config.media_serve_url
    try:
        if config.media_serve_url:
            from .fileserver import MediaFileServer
            media_server = MediaFileServer(config.media_files_root)
            media_server.start()
            serve_url = (f"{config.media_serve_url.rstrip('/')}:"
                         f"{media_server.port}")
    except Exception:  # noqa: BLE001
        logging.getLogger("qq_adapter").exception("media server start failed")

    executor = ActionExecutor(
        client, submitter, whitelist,
        files_root=config.media_files_root,
        base64_max_bytes=config.media_base64_max_bytes,
        serve_url=serve_url,
        core_url=config.core_url,
        core_api_key=config.core_api_key,
        unread_threshold=config.unread_threshold,
        platform=config.platform,
        actions=config.actions,
        attention=attention)
    runtime.executor = executor

    callback = CallbackServer(
        executor,
        transfer_max_bytes=config.transfer.max_bytes,
        auth_token=config.core_api_key,
        host=config.callback_host, port=config.callback_port)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        executor.attach_loop(loop)
        ctx.client = client
        ctx.loop = loop
        callback.start()
        # 把回调端口写到 state_dir（agent / actions 查询用）
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, "port"), "w") as fh:
                fh.write(str(callback.port))
        except Exception:  # noqa: BLE001
            pass
        await client.start()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger("qq_adapter").info("adapter stopped")
    finally:
        callback.stop()
        if media_server is not None:
            media_server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
