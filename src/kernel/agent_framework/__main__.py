"""Entry point: assemble the Agent Framework from config and run it.

::

    python -m kernel.agent_framework --config config/agent.yaml

Every parameter comes from the config file; the agent loop blocks here.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from kernel.agent_framework.agent import Agent
from kernel.scheduler.beat import Beat, load_beat_config
from kernel.agent_framework.config import (RuntimeConfig,
    load_dotenv, load_yaml)
from kernel.agent_framework.context import ContextStore
from kernel.osdb.db import Database
from kernel.http_interface import start_server, StreamBus
from kernel.http_interface.events_port import start_events_port
from kernel.agent_framework.email_tools import (load_email_config,
                                                make_email_receive,
                                                make_email_send)
from kernel.agent_framework.llm.factory import create_llm_client
from kernel.agent_framework.logging_setup import configure_logging, get_logger
from kernel.osdb.messages import MessagesStore
from kernel.osdb.notice import NoticeStore
from kernel.agent_framework.paths import WorkspacePath
from kernel.agent_framework.compression import load_compression_config
from kernel.agent_framework.prompts import make_system_prompt_builder
from kernel.agent_framework.reboot import (REBOOT_NOTICE_MESSAGE,
                                           reboot_tool, restart_pending)
from kernel.agent_framework.skills import make_load_skills
from kernel.agent_framework.sandbox import SandboxConfig, build_executor
from kernel.osdb.system_vars import SystemVarsStore
from kernel.agent_framework.tools import ToolRegistry
from kernel.agent_framework.todo_tools import make_todo_list, make_todo_update
from kernel.agent_framework.notice_tools import (make_notice_create,
                                                 make_notice_delete,
                                                 make_notice_list,
                                                 make_notice_list_cron)
from kernel.agent_framework.image_tools import (ImgBedClient, ImgBedConfig,
                                                ReadImageConfig,
                                                make_readimage)
from kernel.agent_framework.compress_tools import make_context_compress
from kernel.agent_framework.messages_tools import (
    make_messages_clear_unread, make_messages_conversations,
    make_messages_detail, make_messages_download, make_messages_query,
    make_messages_range, make_messages_threads, make_messages_unread)
from kernel.agent_framework.workspace_tools import (
    load_workspace_tools_config, register_workspace_tools)
from kernel.osdb.messages import MessagesStore
from kernel.osdb.notice import NoticeStore
from kernel.osdb.system_vars import SystemVarsStore
from kernel.osdb.todos import TodoStore

logger = get_logger("kernel.agent_framework.main")

def _resolve_session_id(rc: RuntimeConfig, system_vars) -> str:
    """llm.session_id 为空时自动生成 UUID 并持久化（system_vars），
    保证同一运行期内所有请求的 x-opencode-session 稳定。"""
    configured = str(rc.llm.get("session_id", "")).strip()
    if configured:
        return configured
    try:
        stored = system_vars.get("llm_session_id") or ""
        if stored:
            return stored
        generated = uuid.uuid4().hex
        system_vars.set("llm_session_id", generated)
        logger.info("llm session_id generated and persisted")
        return generated
    except Exception:  # noqa: BLE001 — 无存储时退化为进程级随机
        logger.warning("session_id persistence failed; per-process id used")
        return uuid.uuid4().hex


def _with_session(llm_config: dict, rc: RuntimeConfig, system_vars) -> dict:
    """Copy an LLM config and inject the stable ``session_id`` (Console Go
    等网关要求 ``x-opencode-session``；主 llm 与独立压缩 llm 都要带）。"""
    config = dict(llm_config)
    config["session_id"] = _resolve_session_id(rc, system_vars)
    return config


def build_agent(config: dict,
               observer: object = None) -> Agent:
    rc = RuntimeConfig(config)
    db = Database(rc.db_path)
    db.open()
    system_vars = SystemVarsStore(db)
    llm_config = _with_session(rc.llm, rc, system_vars)
    llm = create_llm_client(llm_config)
    registry = ToolRegistry()

    async def echo(text: str) -> str:
        """回显消息（框架自带的示例工具，用于验证 ReAct 循环）。"""
        return text

    registry.register("echo", echo, description="echo the given text back")

    # 数据层（os.db）：exec_async 完成通知等需要 notices
    messages = MessagesStore(db)
    notices = NoticeStore(db)
    todos = TodoStore(rc.todos_file)

    # workspace 路径映射（宿主 ↔ 容器，带防逃逸）——工具层的基础
    workspace = WorkspacePath(
        host_root=rc.workspace_host_root,
        container_root=rc.workspace_container_path)

    # workspace 工具（exec/exec_async + read/edit/replace/append/glob/grep）
    # 全部经 executor 在运行时内执行：sandbox=容器 exec（路径天然隔离），
    # host=系统 bash/python（后续开发内容）
    executor = None  # 执行器 = 路径解析的唯一权威（runtime_path）
    workspace_tools_cfg = load_workspace_tools_config(config)
    if workspace_tools_cfg.enabled:
        executor = build_executor(
            workspace_tools_cfg.mode,
            SandboxConfig(
                container=workspace_tools_cfg.container,
                exec_timeout_seconds=(
                    workspace_tools_cfg.exec_default_timeout_seconds),
                async_workers=workspace_tools_cfg.exec_async_workers,
                async_timeout_seconds=(
                    workspace_tools_cfg.exec_async_timeout_seconds),
                async_log_dir=workspace_tools_cfg.exec_async_log_dir,
            ),
            workspace=workspace,
            notices=notices,
        )
        register_workspace_tools(registry, executor, workspace_tools_cfg)
        logger.info("workspace tools registered (mode=%s, runtime=%s, tools=%s)",
                    workspace_tools_cfg.mode,
                    workspace_tools_cfg.container
                    if workspace_tools_cfg.mode == "sandbox" else "host",
                    [t for t in registry.names()
                     if t.startswith("workspace_")])

    # 系统提示词用 builder：每次组装消息时从注册表实时渲染工具清单。
    # 注册表是唯一事实来源——此处之后的注册（readimage/todo/notice/
    # messages/email/…）也会出现在提示词里，静态工具清单永不过期。
    system_prompt = make_system_prompt_builder(
        project_name=rc.runtime_name,
        model=rc.model,
        user_dir=rc.workspace_container_path,
        prompt_dir=rc.prompt_dir,
        files=rc.prompt_files,
        tool_schemas_provider=registry.schemas,
        skills_dir=rc.skills_directory if rc.skills_enabled else None,
    )

    compression_cfg = load_compression_config(config)
    # 独立压缩模型：compression.llm.enabled=true 时单独创建（记忆提取 +
    # 摘要走它）；否则 None，Agent 压缩时回退主 llm。
    compression_llm = None
    if compression_cfg is not None and compression_cfg.llm is not None:
        # 压缩 llm 与主 llm 共用稳定 session（否则 Console Go 400
        # MissingSessionID：压缩/记忆提取全部失败、只剩 fallback checkpoint）
        compression_llm = create_llm_client(
            _with_session(compression_cfg.llm, rc, system_vars))
        logger.info("compression uses dedicated llm (model=%s)",
                    compression_cfg.llm.get("model", ""))

    agent = Agent(
        llm=llm,
        registry=registry,
        context=ContextStore(rc.context_file),
        system_prompt=system_prompt,
        tool_timeout_seconds=rc.tool_timeout_seconds,
        event_batch_wait_seconds=rc.event_batch_wait_seconds,
        repair_latest_n=rc.repair_latest_n,
        trailing_empty_user=rc.trailing_empty_user,
        user_merge_enabled=rc.user_merge_enabled,
        user_merge_separator=rc.user_merge_separator,
        user_merge_notice=rc.user_merge_notice,
        system_vars=system_vars,
        observer=observer,
        compression=compression_cfg,
        compression_llm=compression_llm,
        notices=notices,
        wake_initial_probability=rc.wake_initial_probability,
        wake_min_probability=rc.wake_min_probability,
        model_output_threshold_chars=rc.model_output_threshold_chars,
        model_output_line_max_chars=rc.model_output_line_max_chars,
        context_max_bytes=rc.context_max_bytes,
        llm_failed_backoff_seconds=rc.llm_failed_backoff_seconds,
        llm_permanent_fail_threshold=rc.llm_permanent_fail_threshold,
        llm_permanent_fail_backoff_seconds=(
            rc.llm_permanent_fail_backoff_seconds),
        tool_result_max_chars=rc.tool_result_max_chars,
        truncated_dir=(workspace_tools_cfg.truncated_dir
                       if workspace_tools_cfg.enabled else ""),
    )
    agent.workspace = workspace  # 供后续模块/工具访问（到 host 转换的唯一入口）
    # 统一"内核产物 -> agent 可用路径"解析器（沙箱=容器 realpath，
    # 宿主机=bash realpath）；工具/beat/压缩提醒的落盘路径都走它
    agent.runtime_path = executor.runtime_path if executor is not None else None
    agent._messages = messages  # HTTP 消息端点使用
    agent._notices = notices    # HTTP notice 端点使用
    agent._todos = todos        # todo 工具 / beat 唤醒使用
    # readimage 工具（本地 venv + Pillow；模型支持 image 时把图片读入对话）
    imgbed_client = None
    if rc.imgbed_enabled:
        imgbed_client = ImgBedClient(ImgBedConfig(
            enabled=True,
            upload_url=rc.imgbed_upload_url,
            api_key=rc.imgbed_api_key,
            api_key_header=rc.imgbed_api_key_header,
            form_field=rc.imgbed_form_field,
            response_url_field=rc.imgbed_response_url_field,
            timeout_seconds=rc.imgbed_timeout_seconds,
            retries=rc.imgbed_retries,
        ))
        agent._imgbed = imgbed_client  # HTTP /api/image 前端上传使用
    if rc.readimage_enabled:
        image_cfg = ReadImageConfig(
            enabled=True,
            mode=rc.readimage_mode,
            max_input_bytes=rc.readimage_max_input_bytes,
            max_edge=rc.readimage_max_edge,
            jpeg_quality=rc.readimage_jpeg_quality,
            quality_floor=rc.readimage_quality_floor,
            max_base64_bytes=rc.readimage_max_base64_bytes,
            min_base64_bytes=rc.readimage_min_base64_bytes,
            block_overhead_bytes=rc.readimage_block_overhead_bytes,
            allow_url=rc.readimage_allow_url,
            allow_private_hosts=rc.readimage_allow_private_hosts,
            url_timeout_seconds=rc.readimage_url_timeout_seconds,
        )
        registry.register("readimage",
                          make_readimage(agent, workspace, image_cfg,
                                         tuple(rc.llm_input),
                                         imgbed=imgbed_client))
        logger.info("readimage enabled (mode=%s max_base64=%d)",
                    image_cfg.mode, image_cfg.max_base64_bytes)
    # context_compress 工具（模型主动请求压缩；实际压缩在安全点执行）
    if compression_cfg is not None:
        registry.register("context_compress",
                          make_context_compress(system_vars))
    # todo 工具（agent 自主维护待办树）
    registry.register("todo_update", make_todo_update(todos))
    registry.register("todo_list", make_todo_list(todos))
    # notice 工具（agent 自主安排定时/通知，到期由 beat 推送）
    registry.register("notice_create", make_notice_create(notices))
    registry.register("notice_list",
                      make_notice_list(
                          notices,
                          runtime_path=(executor.runtime_path
                                        if executor is not None else None),
                          truncated_dir=(workspace_tools_cfg.truncated_dir
                                         if workspace_tools_cfg.enabled
                                         else "")))
    registry.register("notice_delete", make_notice_delete(notices))
    registry.register("notice_list_cron", make_notice_list_cron(notices))
    # 消息工具（agent 自主查询/筛选消息）
    mt_cfg = dict(rc.messages_tools)
    mt_cfg["unread_window_hours"] = rc.unread_window_hours
    registry.register("messages_query",
                      make_messages_query(messages, mt_cfg))
    registry.register("messages_conversations",
                      make_messages_conversations(messages, mt_cfg))
    registry.register("messages_unread",
                      make_messages_unread(messages, mt_cfg))
    registry.register("messages_clear_unread",
                      make_messages_clear_unread(messages, mt_cfg))
    registry.register("messages_threads",
                      make_messages_threads(messages, mt_cfg))
    registry.register("messages_range",
                      make_messages_range(messages, mt_cfg))
    registry.register("messages_detail",
                      make_messages_detail(messages, mt_cfg))
    registry.register("messages_download",
                      make_messages_download(
                          messages, mt_cfg["download_dir"],
                          runtime_path=(executor.runtime_path
                                        if executor is not None else None)))
    # skills_load 工具（按需加载 SKILL.md 全文）
    if rc.skills_enabled:
        registry.register("skills_load",
                          make_load_skills(rc.skills_directory))
    # 系统重启工具（signal -> main() execv 重启）
    registry.register("system_reboot", reboot_tool)

    # 游离文本检测暂停工具（agent 线程维护，写入 system_vars 持久化）
    def free_text_pause(seconds: float = 30) -> str:
        """暂停游离文本（自说自话）检测 x 秒，到期自动恢复。

        参数:
          seconds: 暂停秒数（默认 30）。

        用途: 在需要连续游离思考/输出（不发送消息也不调工具）的回合
        临时关闭提醒，避免误报；屏蔽期间既不通知也不记录。
        """
        return agent.pause_free_text(seconds)

    registry.register("free_text_pause", free_text_pause)
    # email 工具（kernel 内置邮件通道，enabled=false 时不注册）
    email_cfg = load_email_config(config)
    if email_cfg is not None:
        registry.register("email_send", make_email_send(email_cfg))
        registry.register("email_receive", make_email_receive(email_cfg))
        logger.info("email tools registered (smtp=%s imap=%s)",
                    email_cfg.smtp_host, email_cfg.imap_host)
    # beat：kernel 世界时钟 / 调度循环（统一经 notice 通道推送 agent）
    beat_cfg = load_beat_config(config)
    if beat_cfg.enabled:
        agent._beat = Beat(agent, messages, notices, system_vars, beat_cfg,
                           todos=todos,
                           truncated_dir=(workspace_tools_cfg.truncated_dir
                                          if workspace_tools_cfg.enabled
                                          else ""))
        logger.info("beat enabled (interval=%ss)", beat_cfg.interval_seconds)
    agent._db = db  # held for lifetime; closed on shutdown
    return agent

def main() -> int:
    parser = argparse.ArgumentParser(description="Resident Agent Framework")
    parser.add_argument("--config", default="config/agent.yaml")
    args = parser.parse_args()

    load_dotenv(".env")  # 本地调试环境（.gitignore 已忽略）
    config = load_yaml(args.config)
    configure_logging(str(config["runtime"]["log_level"]))
    rc = RuntimeConfig(config)
    bus = StreamBus(history_limit=rc.stream_history)
    agent = build_agent(config, observer=bus.publish)
    logger.info("agent framework assembled (name=%s, tools=%s)",
                config["runtime"]["name"], agent.tools.names())

    async def serve() -> None:
        # 重启恢复：上次 system_reboot 触发的重启后，注入继续消息
        reboot_marker = Path(rc.data_dir) / ".reboot_pending"
        if reboot_marker.exists():
            try:
                reboot_marker.unlink()
            except OSError as exc:
                logger.warning("reboot marker unlink failed: %s", exc)
            agent.push_event({
                "role": "user", "content": REBOOT_NOTICE_MESSAGE,
                "kind": "reboot",
            })
            logger.info("reboot recovery: continue message injected")
        skills_http = None
        if rc.skills_enabled:
            skills_http = {
                "directory": rc.skills_directory,
                "detail_max_chars": rc.skills_detail_max_chars,
                "install_max_zip_bytes": rc.skills_install_max_zip_bytes,
                "install_max_total_bytes":
                    rc.skills_install_max_total_bytes,
                "install_max_files": rc.skills_install_max_files,
                "backup_dir": rc.skills_backup_dir,
                "trash_dir": rc.skills_trash_dir,
            }
        runner, site = await start_server(
            agent, bus, rc.http_host, rc.http_port,
            api_key=rc.http_api_key,
            messages=getattr(agent, "_messages", None),
            notices=getattr(agent, "_notices", None),
            max_body_bytes=rc.http_max_body_bytes,
            skills=skills_http,
            shutdown_timeout=rc.http_shutdown_timeout_seconds)
        logger.info("http interface: http://%s:%d/ (SSE /api/stream)",
                    rc.http_host, rc.http_port)
        beat = getattr(agent, "_beat", None)
        if beat is not None:
            beat.start(asyncio.get_running_loop())
        events_runner = None
        if rc.events_port_enabled:
            events_runner, events_site = await start_events_port(
                beat, rc.events_port_host, rc.events_port_port,
                api_key=rc.events_port_api_key,
                shutdown_timeout=rc.http_shutdown_timeout_seconds)
            logger.info("events port: http://%s:%d/ (POST /api/v1/events)",
                        rc.events_port_host, rc.events_port_port)
        try:
            await agent.run()
        finally:
            if beat is not None:
                beat.stop()
            # 先主动结束所有 SSE 流，再收 runner：否则 aiohttp 会等
            # shutdown_timeout（实测 x2）才取消长连接，重启白等两分钟
            bus.close()
            if events_runner is not None:
                await events_runner.cleanup()
            await runner.cleanup()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        agent.stop()
        db = getattr(agent, "_db", None)
        if db is not None:
            db.close()

    # system_reboot 信号：写标记 + execv 重启（新配置/skills/提示词生效）
    if restart_pending():
        try:
            marker = Path(rc.data_dir) / ".reboot_pending"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1", encoding="utf-8")
        except OSError as exc:
            logger.error("reboot marker write failed: %s", exc)
        logger.info("rebooting via execv (config=%s)", args.config)
        os.execv(sys.executable,
                 [sys.executable, "-m", "kernel.agent_framework",
                  "--config", args.config])
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
