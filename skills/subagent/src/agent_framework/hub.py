"""subagent 常驻 HTTP 入口（设计约定）。

管理一组 agent 实例，每个实例一个后台 asyncio loop 线程：
- 注册（spawn）：创建 context 目录 + 空 context 文件，然后后台起 agent.run()
  常驻；之后可向其 push 事件唤醒。
- 关闭（close）：stop agent + 停掉后台线程。
- 发送 prompt：向指定 agent 实例 push 一条 user 消息。

HTTP 端点（JSON）：
    GET  /api/status                       → 所有实例状态
    POST /api/agents                       → 注册实例 {name, system_prompt, workdir}
    GET  /api/agents/<name>/context        → 读实例的 context.jsonl（分页）
    POST /api/agents/<name>/prompt         → 发 prompt {content}
    POST /api/agents/<name>/close          → 关闭实例
    POST /api/agents/<name>/restore        → 回滚 {scope: workspace|conv|all, ref?}
    POST /api/agents/<name>/switch_model   → 切换模型 {model_profile}
    POST /api/agents/<name>/compress       → 手动触发上下文压缩（idle 时）

端口从 config.yaml 的 hub 段读取（host/port 可配）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from kernel.agent_framework.agent import Agent
from kernel.agent_framework.context import ContextStore
from kernel.agent_framework import checkpoint
from kernel.agent_framework.llm.factory import create_llm_client
from kernel.agent_framework.logging_setup import configure_logging, get_logger
from kernel.agent_framework.tools import ToolRegistry

logger = get_logger("kernel.agent_framework.hub")

DEFAULT_SYSTEM_PROMPT = (
    "你是 subagent，一个任务型助手。收到输入后正常处理并回应。")

def session_dir_workdir(sess_dir: Path) -> Optional[str]:
    """从 session 目录的 session.json 读 workdir；读不到返回 None。"""
    meta_path = Path(sess_dir) / "session.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("workdir")
    except Exception:  # noqa: BLE001 — 读不出就当无 workdir
        return None


DEFAULT_AGENT_PARAMS = dict(
    tool_timeout_seconds=300.0,
    event_batch_wait_seconds=0.5,
    repair_latest_n=5,
    trailing_empty_user=False,
    user_merge_enabled=True,
    user_merge_separator="\n\n---\n\n",
    user_merge_notice="[系统提示] 以下为本次批内合并的多条消息，请注意逐条回应。",
)


class AgentInstance:
    """一个后台常驻的 agent 实例（独立线程 + asyncio loop）。"""

    def __init__(self, name: str, agent: Agent, context: ContextStore,
                 ctx_path: Path, model_profile: str,
                 workdir: Optional[str] = None,
                 approval_broker: Optional[object] = None) -> None:
        self.name = name
        self.agent = agent
        self.context = context
        self.ctx_path = ctx_path
        self.model_profile = model_profile
        self.workdir = workdir
        self._approval_broker = approval_broker
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        assert self.thread is None, f"agent '{self.name}' 已在运行"
        self.thread = threading.Thread(
            target=self._loop_runner, name=f"agent-{self.name}", daemon=True)
        self.thread.start()

    def _loop_runner(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            from kernel.agent_framework import workspace_tools
            workspace_tools.set_session_state(
                self.name, self.workdir, self._approval_broker)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set session state failed %s: %s", self.name, exc)
        # 设置局部 skill 根（workdir/.skill），供 load_skill 在本线程解析。
        try:
            from kernel.agent_framework import skill_tools
            local_root = None
            if self.workdir:
                local_root = Path(self.workdir) / ".skill"
            skill_tools.set_session_skill_root(local_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set skill root failed %s: %s", self.name, exc)
        try:
            self.loop.run_until_complete(self.agent.run())
        finally:
            self.loop.close()
            self.loop = None

    def push(self, event: dict[str, Any]) -> str:
        return self.agent.push_event(event)

    def stop(self) -> None:
        """停止 agent 并回收后台线程。"""
        self.agent.stop()  # 置 _stop + close 队列 → run() 的 wait_nonempty 退出
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self.thread = None

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.agent.state.value,
            "round": self.agent.round_number,
            "context_size": len(self.context.load()),
            "context_path": str(self.ctx_path),
            "model_profile": self.model_profile,
        }


class AgentManager:
    """注册表：管理多个 agent 实例。"""

    def __init__(self, llm_profiles: dict[str, dict[str, Any]],
                 default_profile: str,
                 agent_cfg: dict[str, Any],
                 system_prompt: str,
                 context_root: Path,
                 registry: Optional[ToolRegistry] = None,
                 prompt_dir: Optional[str] = None,
                 prompt_files: Optional[list[str]] = None,
                 project_name: str = "subagent",
                 user_dir: str = "/home/sandbox",
                 notice_hook: Optional[Callable[[str, dict], None]] = None,
                 notice_enabled: bool = True,
                 notice_cfg: Optional[dict] = None,
                 compression_cfg: Optional[Any] = None,
                 compression_llm: Optional[Any] = None,
                 global_skill_dir: Optional[str] = None,
                 exec_cfg: Optional[dict] = None,
                 inputs_cfg: Optional[dict] = None) -> None:
        self.llm_profiles = llm_profiles
        self.default_profile = default_profile
        self.notice_enabled = notice_enabled
        self.notice_cfg = dict(notice_cfg or {})
        # inputs.yaml 声明（{id: spec}）：决定哪些 event 才唤醒用户、推什么文本
        # 未声明 / wakeup:false / channel:none → 只落日志，不推 core notice。
        self.inputs_cfg: dict[str, dict] = {}
        for item in (inputs_cfg or {}).get("inputs", []) or []:
            if isinstance(item, dict) and item.get("id"):
                self.inputs_cfg[str(item["id"])] = item
        self._notice_dedup: dict[str, float] = {}  # id -> 上次推送时间
        self.notice_hook = notice_hook or (
            self._default_notice_hook if notice_enabled else None)
        self.compression_cfg = compression_cfg
        self.compression_llm = compression_llm
        self.global_skill_dir = global_skill_dir
        self.exec_cfg = dict(exec_cfg or {})
        self._exec_approvals: dict[str, dict] = {}  # req_id -> 审批记录
        self._exec_lock = threading.Lock()
        self.agent_cfg = dict(DEFAULT_AGENT_PARAMS)
        self.agent_cfg.update(agent_cfg)
        self.system_prompt = system_prompt
        self.prompt_dir = prompt_dir
        self.prompt_files = prompt_files or []
        self.project_name = project_name
        self.user_dir = user_dir
        # 身份头里的 model 一律读所选 profile 的真实模型，不另硬编码
        self.model = self._profile(default_profile).get("model", "")
        self.context_root = Path(context_root)
        self.context_root.mkdir(parents=True, exist_ok=True)
        self.registry = registry or ToolRegistry()
        self._instances: dict[str, AgentInstance] = {}
        self._lock = threading.Lock()
        self._tool_log_locks: dict[str, threading.Lock] = {}

    def _assemble_prompt(self, override: Optional[str] = None,
                         model: Optional[str] = None,
                         skills: Optional[list[dict]] = None) -> str:
        """系统提示词：显式 override > build_system_prompt 拼接（身份头 +
        config prompt 文件 + 技能清单 + 已注册工具）> 默认。model 取自调用
        方传入（即所选 profile 的真实模型），缺省用 default profile 的。"""
        if override:
            # override 自定义提示词：技能清单独立追加（技能始终暴露）。
            from kernel.agent_framework.prompts import build_skills_section
            skill_section = build_skills_section(skills or [])
            if skill_section:
                return f"{override}\n\n{skill_section}"
            return override
        model = model or self.model
        has_prompt_cfg = bool(self.prompt_dir) and bool(self.prompt_files)
        if has_prompt_cfg:
            from kernel.agent_framework.prompts import build_system_prompt
            schemas = list(self.registry.schemas())  # 已是 schema dict 列表
            return build_system_prompt(
                project_name=self.project_name,
                model=model,  # 所选 profile 的真实模型
                user_dir=self.user_dir,
                prompt_dir=self.prompt_dir,
                files=self.prompt_files,
                tool_schemas=schemas,
                skills=skills,
            )
        return self.system_prompt

    # ── LLM profile 解析 ────────────────────────────────────────────
    def _profile(self, name: str) -> dict[str, Any]:
        """按 profile 名取 llm 配置；不存在抛出 KeyError。"""
        if name not in self.llm_profiles:
            raise KeyError(
                f"模型配置 '{name}' 不存在；可用: {sorted(self.llm_profiles)}")
        return self.llm_profiles[name]

    def _compression_for(self, llm_cfg: dict[str, Any]) -> Any:
        """按 profile 覆盖压缩窗口（按 profile 限制上下文）。

        profile 里可写（只覆盖给出的字段，其余沿用全局 compression）：
            llm_profiles:
              claude:
                compression: {max_tokens: 96000}
        成本分档的模型（如 Claude 在 100K 处单价翻 5 倍）用它把窗口
        压在便宜档内；不写则完全等于全局 compression。
        """
        base = self.compression_cfg
        override = llm_cfg.get("compression")
        if base is None or not isinstance(override, dict):
            return base
        fields = {k: v for k, v in override.items() if hasattr(base, k)}
        return dataclasses.replace(base, **fields) if fields else base

    def _build_agent(self, ctx: ContextStore, ctx_path: Path,
                     profile: str,
                     system_prompt: Optional[str] = None,
                     name: Optional[str] = None,
                     workdir: Optional[str] = None) -> Agent:
        llm_cfg = self._profile(profile)
        compression = self._compression_for(llm_cfg)
        tool_logger = self._make_tool_logger(ctx_path.parent)
        skills = self._scan_skills(workdir, name)
        return Agent(
            llm=create_llm_client(llm_cfg),
            registry=self.registry,
            context=ctx,
            system_prompt=self._assemble_prompt(system_prompt,
                                                 model=llm_cfg.get("model",
                                                                  ""),
                                                 skills=skills),
            tool_timeout_seconds=self.agent_cfg["tool_timeout_seconds"],
            event_batch_wait_seconds=self.agent_cfg[
                "event_batch_wait_seconds"],
            repair_latest_n=self.agent_cfg["repair_latest_n"],
            trailing_empty_user=self.agent_cfg["trailing_empty_user"],
            user_merge_enabled=self.agent_cfg["user_merge_enabled"],
            user_merge_separator=self.agent_cfg["user_merge_separator"],
            user_merge_notice=self.agent_cfg["user_merge_notice"],
            observer=self._observer_for(name),
            name=name or "subagent",
            compression=compression,
            compression_llm=self.compression_llm,
            tool_logger=tool_logger,
            failure_cfg=self.agent_cfg.get("failure_backoff", {}),
        )

    def _scan_skills(self, workdir: Optional[str],
                     name: Optional[str]) -> list[dict]:
        """扫描实例技能清单：局部（workdir/.skill/）+ 全局（config/skill/），
        转成 dict 列表供系统提示词展示。"""
        from kernel.agent_framework import skills as _skills
        local_root = None
        if workdir:
            local_root = Path(workdir) / ".skill"
        global_root = Path(self.global_skill_dir) if self.global_skill_dir \
            else None
        infos = _skills.scan_skills(local_root, global_root)
        return [{"name": i.name, "scope": i.scope,
                 "meta": ({"description": i.meta.description,
                           "trigger": i.meta.trigger}
                          if i.meta else {})}
                for i in infos]

    def _make_tool_logger(self, sess_dir: Path) -> Optional[Callable]:
        """构建工具调用审计日志回调：追加写入 ``<sess_dir>/tools.log``，
        每行一条 JSON {time, tool, params, call_id}。线程安全。"""
        if sess_dir is None:
            return None
        log_path = sess_dir / "tools.log"
        lock = self._tool_log_locks.setdefault(
            str(sess_dir), threading.Lock())

        def hook(record: dict[str, Any]) -> None:
            line = json.dumps(record, ensure_ascii=False)
            with lock:
                try:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError as exc:  # noqa: BLE001
                    logger.warning("tool log write failed %s: %s",
                                   log_path, exc)

        return hook

    def _default_notice_hook(self, inst_name: str,
                             event: dict[str, Any]) -> None:
        """默认 notice 转发：经 core HTTP 的 /api/notice 创建通知，推给
        main agent（用户）。注意清代理直连 host.docker.internal。

        ★ v2（2026-10-13）：先过 config/inputs.yaml 声明——未声明、
        wakeup:false 或 channel:none 的 event 只落日志、不推 core；
        声明了才按 prompt 模板渲染推送（去重按 dedup_s）。
        """
        import os
        import time
        import urllib.request
        kind = str(event.get("kind", ""))
        spec = self.inputs_cfg.get(kind)
        if self.inputs_cfg:
            if spec is None:
                logger.info("notice 未在 inputs.yaml 声明，忽略 kind=%s",
                            kind)
                return
            if not spec.get("wakeup", False) or str(
                    spec.get("channel", "notice")) == "none":
                logger.info("notice 被 inputs.yaml 关闭，忽略 kind=%s", kind)
                return
            dedup_s = float(spec.get("dedup_s", 0) or 0)
            if dedup_s > 0:
                now = time.time()
                last = self._notice_dedup.get(kind, 0.0)
                if now - last < dedup_s:
                    logger.info("notice 去重（dedup_s=%.0f）kind=%s", dedup_s,
                                kind)
                    return
                self._notice_dedup[kind] = now
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(var, None)
        key_file = Path("/home/sandbox/.secrets/core_api_key.txt")
        try:
            token = key_file.read_text(encoding="utf-8").strip().split("=")[
                -1].strip() if key_file.exists() else ""
        except Exception:  # noqa: BLE001
            token = ""
        if not token:
            logger.warning("core_api_key 缺失，notice 未发送: %s",
                           event.get("kind"))
            return
        # checkpoint：只在"最终输出"（completed，本轮最后一条 assistant 无工具
        # 调用 = 真正汇报给用户）时产生并报告，检查点应只有一个（设计约定
        # 批评：现在每次 notice(compressed/tool_error/completed) 都 save 产生了
        # 一堆检查点）。ask 的 cancel/超时(403)属中断，不算本轮结束，不存。
        latest_cp = ""
        if event.get("kind") == "completed":
            try:
                sess_dir = self.context_root / inst_name
                wd = session_dir_workdir(sess_dir)
                if wd and (sess_dir / checkpoint.WS_GIT_DIR).exists():
                    # 增量 add：只提交本 agent 本轮真正改过的文件
                    # （workdir 可能是十万级文件的大目录，全量 add -A 会卡死；
                    #   约定「按增量」。）
                    from kernel.agent_framework import workspace_tools as _wst
                    touched = _wst.pop_touched(wd)
                    cp = checkpoint.save(sess_dir, wd, label="notice",
                                         paths=touched)
                    if cp.get("conv_commit") or cp.get("work_commit"):
                        logger.info(
                            "checkpoint saved: work=%s conv=%s stats=%s",
                            cp.get("work_commit"), cp.get("conv_commit"),
                            cp.get("stats_file"))
                    if cp.get("latest"):
                        lc = cp["latest"]
                        latest_cp = (f"[checkpoint] {lc['time']} "
                                     f"work={lc['work_commit']}")
            except Exception as exc:  # noqa: BLE001 — checkpoint 失败不影响 notice
                logger.warning("checkpoint save failed %s: %s", inst_name, exc)
        if kind == "completed":
            content = (f"subagent 实例「{inst_name}」完成任务。\n"
                       f"返回结果：\n{event.get('model_output', '')}")
        elif kind == "tool_error":
            content = (f"subagent 实例「{inst_name}」工具 "
                       f"「{event.get('tool')}」异常返回 "
                       f"code={event.get('code')}，可继续执行。")
        else:
            content = event.get("message", "")
        # ★ v2：inputs.yaml 声明了 prompt 模板 → 按模板渲染（变量缺失留空）
        if spec and spec.get("prompt"):
            import re as _re
            fields = {
                "agent": inst_name,
                "summary": str(event.get("model_output", "") or
                               event.get("message", "")),
                "message": str(event.get("message", "") or ""),
                "command": str(event.get("command", "") or ""),
                "reason": str(event.get("reason", "") or ""),
                "request_id": str(event.get("request_id", "") or ""),
                "tool": str(event.get("tool", "") or ""),
                "error": str(event.get("message", "") or ""),
                "code": str(event.get("code", "") or ""),
                "kind": kind,
            }
            content = _re.sub(r"\{(\w+)\}",
                              lambda m: fields.get(m.group(1), m.group(0)),
                              str(spec["prompt"])).strip()
        if latest_cp:
            content = f"{content}\n\n{latest_cp}"
        payload = json.dumps({
            "content": content,
            "due_at": "",  # 立即触发
            "kind": "once",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.notice_url,
            data=payload,
            headers={"X-Api-Token": token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.notice_timeout_s) as resp:
                logger.info("notice sent kind=%s agent=%s -> %s",
                            kind, inst_name, resp.status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notice send failed kind=%s: %s", kind, exc)

    def _observer_for(self, name: Optional[str]) -> Optional[Callable]:
        """为实例构建 observer：拦截 notice 事件转发到用户。
        observer 为 None 时不转发（纯运行）。"""
        if self.notice_hook is None:
            return None
        inst_name = name or "subagent"

        def observer(event: dict[str, Any]) -> None:
            if event.get("type") == "notice":
                try:
                    self.notice_hook(inst_name, event)
                except Exception:  # noqa: BLE001 — 通知失败不影响 agent 循环
                    logger.warning("notice hook failed: %s",
                                   event.get("kind"))

        return observer

    def _ctx(self, session: str,
             workdir: Optional[str] = None) -> tuple[ContextStore, Path]:
        """session 的 context 存取。目录 = context_root/{session}/，
        内含 context.jsonl（唯一真相源）+ session.json（元数据，目前存
        workdir 工作目录位置；workdir 缺失时复用已有的）。"""
        import json as _json
        from datetime import datetime
        sess_dir = self.context_root / session
        sess_dir.mkdir(parents=True, exist_ok=True)
        ctx_path = sess_dir / "context.jsonl"
        if not ctx_path.exists():
            ctx_path.touch()  # 只创建空 context 文件
        meta_path = sess_dir / "session.json"
        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        else:
            meta = {}
        if workdir is not None:
            meta["workdir"] = workdir
        meta.setdefault("session", session)
        meta.setdefault("created_at",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        meta_path.write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return ContextStore(ctx_path), ctx_path

    def profiles(self) -> list[str]:
        return sorted(self.llm_profiles)

    def spawn(self, session: str, system_prompt: Optional[str] = None,
              model_profile: Optional[str] = None,
              workdir: Optional[str] = None) -> dict:
        profile = model_profile or self.default_profile
        with self._lock:
            if session in self._instances:
                return {"ok": False, "error": f"agent '{session}' 已存在"}
            try:
                ctx, ctx_path = self._ctx(session, workdir)
                # 初始化 checkpoint 双 git（工作区分离 git + 会话 git）
                sess_dir = ctx_path.parent
                wd = workdir or session_dir_workdir(sess_dir)
                if wd:
                    # 09-05 修复：workdir 不存在时自动创建，否则 agent 第一条
                    # exec（如 pwd && ls）会因目录缺失直接 500（匿名模型_wordle 实证）。
                    Path(wd).mkdir(parents=True, exist_ok=True)
                    try:
                        checkpoint.init_repos(sess_dir, wd)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("checkpoint init 失败 %s: %s",
                                       session, exc)
                agent = self._build_agent(ctx, ctx_path, profile,
                                          system_prompt, name=session,
                                          workdir=wd)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}
            inst = AgentInstance(session, agent, ctx, ctx_path, profile,
                                 workdir=wd, approval_broker=self)
            inst.start()
            self._instances[session] = inst
            # 若本地已有历史上下文，提示调用方这是旧 agent，需确认上下文
            # 与预期一致（不阻断复用，仅提示）。
            existing = self._existing_ctx_count(ctx_path)
            resp: dict = {"ok": True, "agent": inst.status()}
            if existing > 0:
                resp["warning"] = (
                    f"这是一个旧 agent：'{session}' 已有 {existing} 条历史上下文"
                    f"（{ctx_path}），请确认与你的预期一致后继续。")
            logger.info("spawned agent '%s' (profile=%s context=%s)"
                        "%s", session, profile, ctx_path,
                        f" existing_ctx={existing}" if existing else "")
            return resp

    def _existing_ctx_count(self, ctx_path: Path) -> int:
        """返回 context 文件已累积的历史消息条数（0=无历史）。"""
        if not ctx_path.exists() or ctx_path.stat().st_size == 0:
            return 0
        from kernel.agent_framework.context import ContextStore
        try:
            return len(ContextStore(ctx_path).load())
        except Exception:  # noqa: BLE001 — 读不出就当无历史
            return 0

    def switch_model(self, name: str, model_profile: str,
                     system_prompt: Optional[str] = None) -> dict:
        """切换实例的模型：stop 实例 → 用新 profile 重建 Agent（复用同一
        context/ctx_path）→ 重新后台 start。"""
        with self._lock:
            inst = self._instances.get(name)
            if inst is None:
                return {"ok": False, "error": f"agent '{name}' 不存在"}
            context = inst.context
            ctx_path = inst.ctx_path
            inst.stop()  # 停线程 + 回收
            try:
                agent = self._build_agent(context, ctx_path, model_profile,
                                          system_prompt, name=name,
                                          workdir=inst.workdir)
            except KeyError as exc:
                # 构建失败：用原 profile 恢复原实例，保证不丢
                agent = self._build_agent(context, ctx_path,
                                          inst.model_profile, system_prompt,
                                          name=name, workdir=inst.workdir)
                new_inst = AgentInstance(name, agent, context, ctx_path,
                                         inst.model_profile,
                                         workdir=inst.workdir)
                new_inst.start()
                self._instances[name] = new_inst
                return {"ok": False, "error": str(exc)}
            new_inst = AgentInstance(name, agent, context, ctx_path,
                                     model_profile, workdir=inst.workdir)
            new_inst.start()
            self._instances[name] = new_inst
            logger.info("switched agent '%s' %s -> %s (context 保留)",
                        name, inst.model_profile, model_profile)
            return {"ok": True,
                    "old_profile": inst.model_profile,
                    "agent": new_inst.status()}

    def close(self, name: str) -> dict:
        with self._lock:
            inst = self._instances.pop(name, None)
        if inst is None:
            return {"ok": False, "error": f"agent '{name}' 不存在"}
        inst.stop()
        logger.info("closed agent '%s'", name)
        return {"ok": True}

    def prompt(self, name: str, content: str) -> dict:
        inst = self._instances.get(name)
        if inst is None:
            return {"ok": False, "error": f"agent '{name}' 不存在"}
        if not content or not str(content).strip():
            return {"ok": False, "error": "content 不能为空"}
        event_id = inst.push({"role": "user", "content": str(content)})
        return {"ok": True, "event_id": event_id}

    def context(self, name: str, limit: int = 50) -> dict:
        inst = self._instances.get(name)
        if inst is None:
            return {"ok": False, "error": f"agent '{name}' 不存在"}
        entries = inst.context.load()
        return {"ok": True, "total": len(entries),
                "entries": entries[-limit:]}

    def restore(self, name: str, scope: str,
                ref: Optional[str] = None) -> dict:
        """回滚某实例：scope=workspace(只工作区)/conv(只会话记录)/
        all(都回滚)；ref 为 git ref（缺省回滚到上一 checkpoint HEAD~1）。
        """
        inst = self._instances.get(name)
        if inst is None:
            return {"ok": False, "error": f"agent '{name}' 不存在"}
        sess_dir = inst.ctx_path.parent
        wd = session_dir_workdir(sess_dir)
        if not wd:
            return {"ok": False, "error": (f"agent '{name}' 无 workdir"
                                           "（session.json 缺失）")}
        if not (sess_dir / checkpoint.WS_GIT_DIR).exists():
            return {"ok": False, "error": f"agent '{name}' 未初始化 checkpoint"}
        return checkpoint.restore(sess_dir, wd, scope, ref)

    def compress(self, name: str) -> dict:
        """手动触发某实例上下文压缩（idle 时经 HTTP 调用）。未启用 compression
        或实例不在 idle 时拒绝。压缩协程提交到该 agent 自己的 loop 执行。"""
        if self.compression_cfg is None:
            return {"ok": False, "error": "compression 未启用（config 未配置）"}
        inst = self._instances.get(name)
        if inst is None:
            return {"ok": False, "error": f"agent '{name}' 不存在"}
        if inst.loop is None or not inst.loop.is_running():
            return {"ok": False, "error": f"agent '{name}' loop 未运行"}
        if inst.agent.state.value != "idle":
            return {"ok": False, "error": (f"agent '{name}' 非 idle"
                                           f"（当前 {inst.agent.state.value}），"
                                           "暂不能压缩")}
        try:
            future = asyncio.run_coroutine_threadsafe(
                inst.agent._run_compress(force=True), inst.loop)
            summary = future.result(
                timeout=self.compression_cfg.manual_timeout_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("manual compress failed %s: %s", name, exc)
            return {"ok": False, "error": f"压缩失败: {exc}"}
        return {"ok": True, "compressed": summary is not None,
                "summary": (summary or "")[:500]}

    def status_all(self) -> dict:
        with self._lock:
            states = [inst.status() for inst in self._instances.values()]
        return {"ok": True, "agents": states}

    # ── exec 命令审批 broker（exec 工具在 agent 线程调用，阻塞等审批）──
    @property
    def notice_url(self) -> str:
        return str(self.notice_cfg.get(
            "url", "http://host.docker.internal:18086/api/notice"))

    @property
    def notice_timeout_s(self) -> float:
        return float(self.notice_cfg.get("timeout_s", 6))

    @property
    def exec_whitelist(self) -> list[str]:
        return list(self.exec_cfg.get("whitelist") or [])

    @property
    def exec_timeout_s(self) -> float:
        return float(self.exec_cfg.get("timeout_s", 60))

    @property
    def exec_approval_timeout_s(self) -> float:
        return float(self.exec_cfg.get("approval_timeout_s", 300))

    def request_approval(self, session: str, command: str,
                         reason: str) -> str:
        """exec 工具请求审批：登记 req_id + threading.Event，发 notice 通知
        用户（含 approve/reject 说明）。返回 req_id；失败返回空串。"""
        import uuid
        req_id = f"{session}-{uuid.uuid4().hex[:8]}"
        ev = threading.Event()
        with self._exec_lock:
            self._exec_approvals[req_id] = {
                "session": session, "command": command, "reason": reason,
                "event": ev, "decision": None,
            }
        logger.info("exec approval requested session=%s req_id=%s cmd=%s reason=%s",
                    session, req_id, command, reason)
        # notice 通知用户（含审批说明）
        if self.notice_hook is not None:
            content = (
                f"subagent 实例「{session}」请求执行命令审批：\n"
                f"命令: {command}\n原因: {reason}\n"
                f"请求ID: {req_id}\n\n"
                f"请调用 HTTP 接口审批：\n"
                f"  POST /api/agents/{session}/approve "
                f"{{'request_id':'{req_id}','approved':true}}\n"
                f"  (approved=false 表示拒绝)")
            try:
                self.notice_hook(session, {
                    "type": "notice", "kind": "exec_approval", "agent": session,
                    "request_id": req_id, "command": command, "reason": reason,
                    "approve_hint": (f"POST /api/agents/{session}/approve"
                                     f" {{'request_id':'{req_id}','approved':true}}"),
                    "message": content})
            except Exception as exc:  # noqa: BLE001
                logger.warning("exec approval notice failed: %s", exc)
        return req_id

    def wait_approval(self, req_id: str, timeout: Optional[float] = None) \
            -> tuple[bool, str]:
        """exec 工具阻塞等待审批结果。返回 (是否批准, 拒绝原因)。"""
        with self._exec_lock:
            rec = self._exec_approvals.get(req_id)
        if rec is None:
            return False, "审批请求不存在"
        timeout = timeout if timeout is not None else self.exec_approval_timeout_s
        got = rec["event"].wait(timeout)
        with self._exec_lock:
            rec = self._exec_approvals.get(req_id)
            decision = rec["decision"] if rec else None
            if rec:
                self._exec_approvals.pop(req_id, None)
        if not got:
            return False, "审批超时（未在规定时间内 approve/reject，按拒绝处理）"
        if decision is True:
            return True, ""
        return False, ("审批被拒绝" if decision is False else "审批未完成")

    def resolve_approval(self, name: str, request_id: str,
                         approved: bool) -> dict:
        """HTTP approve 端点用：对指定审批请求给 approve/reject，唤醒等待者。"""
        with self._exec_lock:
            rec = self._exec_approvals.get(request_id)
            if rec is None or rec.get("session") != name:
                return {"ok": False, "error": f"审批请求 {request_id} 不存在"
                                              "或不属于该实例"}
            rec["decision"] = bool(approved)
            rec["event"].set()
        return {"ok": True, "request_id": request_id,
                "approved": bool(approved)}

    # ── ask 问答工具（agent 问用户 N 个问题，阻塞等一次性答复）────────
    def request_ask(self, session: str, questions: list[dict]) -> str:
        """ask 工具请求提问：登记 ask_id + Event，发 notice（含全部题目+选项
        +起草回复）。返回 ask_id。"""
        import uuid
        ask_id = f"{session}-ask-{uuid.uuid4().hex[:8]}"
        ev = threading.Event()
        with self._exec_lock:
            self._exec_approvals[ask_id] = {
                "session": session, "kind": "ask", "questions": questions,
                "event": ev, "decision": None,
            }
        lines = [f"subagent 实例「{session}」向你提问（ask_id={ask_id}）："]
        for i, q in enumerate(questions, 1):
            head = f"[Q{i}] {q.get('question', '')}"
            ans = q.get("answers") or []
            opt = q.get("options")
            if ans:
                head += "\n    选项: " + ", ".join(
                    f"{chr(64 + j)}={a}" for j, a in enumerate(ans, 1))
            if opt:
                head += f"\n    起草回复参考: {opt}"
            lines.append(head)
        lines.append(
            f"\n请一次性答复所有题（POST /api/agents/{session}/ask_reply "
            f"{'{'}ask_id:'{ask_id}', answers:{'{'}每题的选项或自定义串{'}'}{'}'}\n"
            f"或整体取消 cancel=true→工具返回 403不执行本轮)。")
        content = "\n\n".join(lines)
        if self.notice_hook is not None:
            try:
                self.notice_hook(session, {
                    "type": "notice", "kind": "exec_approval", "agent": session,
                    "request_id": ask_id, "command": "ask", "reason": "",
                    "message": content})
            except Exception as exc:  # noqa: BLE001
                logger.warning("ask notice failed %s: %s", session, exc)
        return ask_id

    def wait_ask(self, ask_id: str,
                 timeout: Optional[float] = None) -> tuple[bool, Any, str]:
        """ask 工具阻塞等待用户答复。返回 (是否放行, answers dict, 说明)。
        cancel/超时 → (False, None, 说明)。"""
        with self._exec_lock:
            rec = self._exec_approvals.get(ask_id)
        if rec is None:
            return False, None, "ask 请求不存在"
        timeout = timeout if timeout is not None else self.exec_approval_timeout_s
        got = rec["event"].wait(timeout)
        with self._exec_lock:
            rec = self._exec_approvals.get(ask_id)
            decision = rec["decision"] if rec else None
            if rec:
                self._exec_approvals.pop(ask_id, None)
        if not got:
            return False, None, "ask 超时（用户未在限时内答复，按取消处理）"
        if isinstance(decision, dict):
            if decision.get("_cancel"):
                return False, None, "ask 已被用户取消"
            return True, decision, ""
        return False, None, "ask 答复无效"

    def resolve_ask(self, name: str, ask_id: str, answers: Optional[dict],
                    cancel: bool) -> dict:
        """HTTP ask_reply 端点用：给 ask 一次性答复（answers dict），或 cancel。"""
        with self._exec_lock:
            rec = self._exec_approvals.get(ask_id)
            if rec is None or rec.get("session") != name \
                    or rec.get("kind") != "ask":
                return {"ok": False, "error": f"ask 请求 {ask_id} 不存在"
                                              "或不属于该实例"}
            if cancel:
                rec["decision"] = {"_cancel": True}
            else:
                rec["decision"] = answers or {}
            rec["event"].set()
        return {"ok": True, "ask_id": ask_id,
                "cancel": bool(cancel), "answers": answers or {}}


def _json_send(handler: BaseHTTPRequestHandler, code: int,
               payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HubHandler(BaseHTTPRequestHandler):
    manager: AgentManager = None  # type: ignore[assignment]  # 类级注入
    api_token: str = ""           # 类级注入：非空时要求 X-Api-Token 头

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默 access 日志
        logger.debug("hub access: " + fmt, *args)

    def _auth_ok(self) -> bool:
        """服务鉴权：config hub.api_key 非空时校验 X-Api-Token。
        空 = 不鉴权（默认，只监听 127.0.0.1）。"""
        if not self.api_token:
            return True
        return self.headers.get("X-Api-Token", "") == self.api_token

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if not self._auth_ok():
            _json_send(self, 401, {"ok": False, "error": "缺少/错误 X-Api-Token"})
            return
        try:
            if path == "/api/status":
                _json_send(self, 200, self.manager.status_all())
                return
            if path == "/api/profiles":
                _json_send(self, 200, {"ok": True,
                                       "profiles": self.manager.profiles()})
                return
            parts = path.split("/")
            # /api/agents/<name>/context
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "agents" \
                    and parts[4] == "context":
                import urllib.parse
                query_params = urllib.parse.parse_qs(query)
                limit = int((query_params.get("limit") or ["50"])[0])
                _json_send(self, 200,
                           self.manager.context(parts[3], limit=limit))
                return
            _json_send(self, 404, {"ok": False, "error": f"未知路径 {path}"})
        except Exception as exc:  # noqa: BLE001
            _json_send(self, 500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path
        body = self._read_body()
        if not self._auth_ok():
            _json_send(self, 401, {"ok": False, "error": "缺少/错误 X-Api-Token"})
            return
        try:
            if path == "/api/agents":
                name = body.get("name") or body.get("agent_id")
                if not name:
                    _json_send(self, 400, {"ok": False,
                                           "error": "缺少 name"})
                    return
                result = self.manager.spawn(
                    str(name), body.get("system_prompt"),
                    model_profile=body.get("model_profile"),
                    workdir=body.get("workdir"))
                _json_send(self, 200 if result.get("ok") else 400, result)
                return
            if path == "/api/profiles":
                _json_send(self, 200, {"ok": True,
                                       "profiles": self.manager.profiles()})
                return
            parts = path.split("/")
            # /api/agents/<name>/prompt | close | switch_model
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "agents":
                name, verb = parts[3], parts[4]
                if verb == "prompt":
                    result = self.manager.prompt(name,
                                                 str(body.get("content", "")))
                elif verb == "close":
                    result = self.manager.close(name)
                elif verb == "restore":
                    scope = str(body.get("scope", "all"))
                    ref = body.get("ref")
                    result = self.manager.restore(name, scope,
                                                  ref or None)
                elif verb == "switch_model":
                    profile = body.get("model_profile")
                    if not profile:
                        _json_send(self, 400, {"ok": False,
                                               "error": "缺少 model_profile"})
                        return
                    result = self.manager.switch_model(
                        name, str(profile), body.get("system_prompt"))
                elif verb == "compress":
                    result = self.manager.compress(name)
                elif verb == "approve":
                    request_id = body.get("request_id")
                    if not request_id:
                        _json_send(self, 400, {"ok": False,
                                               "error": "缺少 request_id"})
                        return
                    result = self.manager.resolve_approval(
                        name, str(request_id),
                        bool(body.get("approved", False)))
                elif verb == "ask_reply":
                    ask_id = body.get("ask_id")
                    if not ask_id:
                        _json_send(self, 400, {"ok": False,
                                               "error": "缺少 ask_id"})
                        return
                    result = self.manager.resolve_ask(
                        name, str(ask_id),
                        body.get("answers"),
                        bool(body.get("cancel", False)))
                else:
                    _json_send(self, 404, {"ok": False,
                                           "error": f"未知动作 {verb}"})
                    return
                _json_send(self, 200 if result.get("ok") else 400, result)
                return
            _json_send(self, 404, {"ok": False, "error": f"未知路径 {path}"})
        except Exception as exc:  # noqa: BLE001
            _json_send(self, 500, {"ok": False, "error": str(exc)})


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """递归合并：over 覆盖 base；两边都是 dict 时逐键递归。"""
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(config_path: Any) -> dict[str, Any]:
    """加载配置。

    v2（2026-10-13）支持分层：传 list[Path] 时按顺序深合并（后者覆盖前者），
    典型用法 = [config/default.yaml, config/local.yaml]。传单个 Path 时行为
    同旧版（只读那一个文件），保持向后兼容。

    - ${VAR} 展开为环境变量（不存在的展开为空串）
    - 以 ./ 开头的路径值按 **skill 根目录**（config 目录的上一级）解析
    - 密钥文件按顺序尝试（已存在的环境变量优先）：
      <config 目录>/.env（旧）、$SECRETS_DIR/subagent.env（默认
      /home/sandbox/.secrets/subagent.env）、<skill 根>/.secrets/subagent.env
    """
    import os
    import re

    from dotenv import load_dotenv
    import yaml

    paths = [Path(p) for p in config_path] if isinstance(
        config_path, (list, tuple)) else [Path(config_path)]
    cfg_dir = paths[0].parent
    skill_root = cfg_dir.parent
    # 密钥注入（已存在的环境变量优先，load_dotenv 默认不覆盖）
    secrets_dir = Path(os.environ.get("SECRETS_DIR",
                                      "/home/sandbox/.secrets"))
    for env_file in (cfg_dir / ".env",
                     secrets_dir / "subagent.env",
                     skill_root / ".secrets" / "subagent.env"):
        if env_file.is_file():
            load_dotenv(env_file, override=False)

    raw: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            continue
        raw = _deep_merge(raw, loaded)

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            out = re.sub(r"\$\{(\w+)\}",
                         lambda m: os.environ.get(m.group(1), ""), value)
            if out.startswith("./"):
                out = str(skill_root / out[2:])
            return out
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    return expand(raw)


def load_inputs(path: Path) -> dict[str, Any]:
    """读 config/inputs.yaml（输入声明）。文件不存在 → 返回空 dict，
    此时 notice 不过滤（保持旧行为）。"""
    import yaml
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    here = Path(__file__).resolve().parent.parent.parent  # skill 根
    # 配置分层（v2）：--config a --config b 按顺序深合并；
    # 无参数时默认 default.yaml + local.yaml，都没有才退回老 config.yaml。
    cfg_paths: list[Path] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            cfg_paths.append(Path(argv[i + 1]))
            i += 2
        else:
            cfg_paths.append(Path(argv[i]))  # 兼容旧用法：直接传一个路径
            i += 1
    if not cfg_paths:
        _default = here / "config" / "default.yaml"
        _local = here / "config" / "local.yaml"
        if _default.is_file():
            cfg_paths = [_default] + ([_local] if _local.is_file() else [])
        else:
            cfg_paths = [here / "config" / "config.yaml"]
    cfg = load_config(cfg_paths)
    inputs_cfg = load_inputs(here / "config" / "inputs.yaml")
    configure_logging(cfg.get("runtime", {}).get("log_level", "INFO"))
    logger.info("配置来源: %s", ", ".join(str(p) for p in cfg_paths))
    logger.info("输入声明: %d 条（config/inputs.yaml）",
                len(inputs_cfg.get("inputs", []) or []))

    hub_cfg = cfg.get("hub", {})
    host = hub_cfg.get("host", "127.0.0.1")
    port = int(hub_cfg.get("port", 19711))
    context_root = cfg.get("runtime", {}).get(
        "context_root", "/home/sandbox/subagent_data")

    registry = ToolRegistry()
    # 移除 checkpoint 回滚测试用的临时工具
    # （echo / write_file，任意绝对路径写入无安全约束），正式文件工具
    # 由 workspace_tools（read/write/edit/...+exec）按 session 注册。
    from kernel.agent_framework.skill_tools import register_skill_tools
    global_skill_dir = here / "config" / "skill"
    global_skill_dir.mkdir(parents=True, exist_ok=True)
    register_skill_tools(registry, global_skill_dir)
    logger.info("已注册工具: %s", registry.names())
    logger.info("全局 skill 目录: %s", global_skill_dir)

    # ── compression 配置（可选独立压缩模型）──────────────────────────
    from kernel.agent_framework import compression as _comp
    compression_cfg = _comp.load_compression_config(cfg)
    compression_llm = None
    if compression_cfg is not None and compression_cfg.llm_profile:
        llm_profiles = cfg.get("llm_profiles", {})
        prof = llm_profiles.get(compression_cfg.llm_profile)
        if prof:
            compression_llm = create_llm_client(prof)
            logger.info("compression 用独立模型: %s",
                        compression_cfg.llm_profile)
        else:
            logger.warning("compression llm_profile '%s' 不存在，"
                           "回退主 agent LLM", compression_cfg.llm_profile)
    if compression_cfg is not None:
        logger.info("compression enabled: max_tokens=%d threshold=%.2f",
                    compression_cfg.max_tokens, compression_cfg.threshold)

    manager = AgentManager(
        llm_profiles=cfg.get("llm_profiles", {"default": cfg.get("llm", {})}),
        default_profile="default",
        agent_cfg=cfg.get("agent", {}),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        context_root=context_root,
        registry=registry,
        prompt_dir=str(here / cfg.get("prompt", {}).get(
            "directory", "config/prompt")),
        prompt_files=cfg.get("prompt", {}).get("files", []),
        project_name=cfg.get("project", {}).get("name", "subagent"),
        user_dir=cfg.get("project", {}).get("user_dir", "/home/sandbox"),
        notice_cfg=cfg.get("notice", {}),
        compression_cfg=compression_cfg,
        compression_llm=compression_llm,
        global_skill_dir=str(global_skill_dir),
        exec_cfg=cfg.get("exec", {}),
        inputs_cfg=inputs_cfg,
    )
    HubHandler.manager = manager
    HubHandler.api_token = str(hub_cfg.get("api_key", "") or "")

    # exec 工具需在 manager 创建后注入 broker（审批引用运行时经线程局部拿）
    from kernel.agent_framework.workspace_tools import (
        register_workspace_tools as _reg_ws)
    _reg_ws(registry, context_root, cfg.get("workspace_tools", {}))

    server = ThreadingHTTPServer((host, port), HubHandler)
    logger.info("subagent hub 监听 %s:%d (context_root=%s)",
                host, port, context_root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("hub 收到中断，停止所有 agent")
        for name in list(manager._instances):
            manager.close(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
