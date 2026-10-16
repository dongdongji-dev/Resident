"""Workspace tools — exec + file tools composed through the runtime executor.

All file operations (read / edit / replace / append / write / glob / grep /
rm) execute INSIDE the workspace runtime through ``WorkspaceExecutor.fs_op``
(a fsops JSON job: sandbox -> ``docker exec python3 fsops.py``; host -> the
kernel interpreter). Paths are runtime paths handed over verbatim: in
sandbox mode the container boundary IS the isolation, so container paths
outside the mount (``/tmp/...``) simply work — no path checks are performed.

Interaction design:

- ``workspace_read`` renders ``cat -n`` style line numbers (toggleable) with
  a size ceiling that ERRORS instead of truncating; start/end limit the range.
- ``workspace_edit`` is a precise old/new-string replacement reporting the
  concrete diff back — including DELETED lines (line number + original
  text) — so the agent never deletes blindly. Multiple matches refuse
  without ``replace_all``.
- ``workspace_replace`` swaps rows ``[start_line, end_line]`` (1-based,
  inclusive) for new content, with the same diff reporting.
- ``workspace_glob`` / ``workspace_grep`` cap their output (relative paths,
  head_limit, max-columns) to protect the context window. grep is pure
  Python inside the runtime (no extra dependency).
- ``workspace_rm`` is a two-step confirmation delete (manifest + uuid).

Every threshold value comes from the ``workspace_tools`` YAML section — no
hidden defaults.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.paths import open_file_inside
from kernel.agent_framework.sandbox import WorkspaceExecutor
from kernel.agent_framework.tools import ToolRegistry

logger = get_logger("kernel.agent_framework.workspace_tools")

_TOOL_PREFIX = "workspace_"
_TOOL_CATEGORY = "workspace."

# 写类工具（edit/replace）的读-改-写关键区互斥：并行调用同一文件时
# 串行执行，避免后写者基于旧快照覆盖先写者（丢失更新）。
_WRITE_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class WorkspaceToolsConfig:
    enabled: bool
    # 执行模式：sandbox=全部经容器 exec（路径天然隔离，无路径检查）；
    # host=系统 bash/python（宿主机模式为后续开发内容，安全限制待实现）
    mode: str
    container: str
    exec_default_timeout_seconds: float
    # 前台 exec 实际等待 = min(timeout, 该上限)：阻塞等待只适合短命令
    exec_max_timeout_seconds: float
    exec_output_max_chars: int
    truncated_dir: str
    exec_async_timeout_seconds: float
    exec_async_workers: int
    exec_async_log_dir: str
    # 文件操作（read/edit/glob/grep/rm 等 fsops 调用）单次超时（秒）
    fs_timeout_seconds: float
    read_max_bytes: int
    read_max_lines: int
    read_warn_bytes: int
    read_default_line_numbers: bool
    glob_max_results: int
    glob_timeout_seconds: float
    grep_head_limit: int
    grep_max_columns: int
    edit_max_old_chars: int
    # edit/replace 返回字符串（diff）最大字节：超限截断并附省略提示
    # （返回的是改动区 diff，不占上下文；文件本身不限制大小）
    edit_output_max_chars: int
    write_max_bytes: int
    # workspace_rm：两步确认清单的显示上限（超出部分完整落盘 truncated_dir）
    rm_max_entries: int
    # workspace_rm：确认 uuid 有效期（秒）
    rm_confirm_ttl_seconds: float


def load_workspace_tools_config(config: dict) -> WorkspaceToolsConfig:
    """Build WorkspaceToolsConfig from the ``workspace_tools`` YAML section
    (all keys required; no implicit defaults)."""
    section = config["workspace_tools"]
    mode = str(section["mode"]).strip()
    if mode not in ("sandbox", "host"):
        raise ValueError(
            f"workspace_tools.mode 必须是 sandbox 或 host（当前: {mode!r}）")
    return WorkspaceToolsConfig(
        enabled=bool(section["enabled"]),
        mode=mode,
        container=str(section["container"]),
        exec_default_timeout_seconds=float(
            section["exec_default_timeout_seconds"]),
        exec_max_timeout_seconds=float(
            section["exec_max_timeout_seconds"]),
        exec_output_max_chars=int(section["exec_output_max_chars"]),
        truncated_dir=str(section["truncated_dir"]),
        exec_async_timeout_seconds=float(
            section["exec_async_timeout_seconds"]),
        exec_async_workers=int(section["exec_async_workers"]),
        exec_async_log_dir=str(section["exec_async_log_dir"]),
        fs_timeout_seconds=float(section["fs_timeout_seconds"]),
        read_max_bytes=int(section["read_max_bytes"]),
        read_max_lines=int(section["read_max_lines"]),
        read_warn_bytes=int(section["read_warn_bytes"]),
        read_default_line_numbers=bool(section["read_default_line_numbers"]),
        glob_max_results=int(section["glob_max_results"]),
        glob_timeout_seconds=float(section["glob_timeout_seconds"]),
        grep_head_limit=int(section["grep_head_limit"]),
        grep_max_columns=int(section["grep_max_columns"]),
        edit_max_old_chars=int(section["edit_max_old_chars"]),
        edit_output_max_chars=int(section["edit_output_max_chars"]),
        write_max_bytes=int(section["write_max_bytes"]),
        rm_max_entries=int(section["rm_max_entries"]),
        rm_confirm_ttl_seconds=float(
            section["rm_confirm_ttl_seconds"]),
    )


# ── helpers ────────────────────────────────────────────────────────────────

def _truncate(text: str, max_chars: int) -> str:
    """Truncate tool output so it cannot bloat the context window."""
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（内容过长，已截断至 {max_chars} 字符）"


def _save_artifact(executor: WorkspaceExecutor, truncated_dir: str,
                   text: str, prefix: str) -> str:
    """Persist full (would-be-truncated) content host-side into the cache dir
    (inside the workspace mount, so the agent can read it in either mode);
    returns the runtime path for the agent hint ("" on failure)."""
    if not text or not truncated_dir:
        return ""
    try:
        directory = os.path.abspath(truncated_dir)
        filename = (f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}.txt")
        host = os.path.join(directory, filename)
        with open_file_inside(host, executor.workspace.host_root, "w",
                              create_parents=True,
                              encoding="utf-8") as handle:
            handle.write(str(text))
        return executor.runtime_path(host)
    except Exception:  # noqa: BLE001 — 缓存失败不掩盖主结果
        return ""


def _truncate_with_save(executor: WorkspaceExecutor, truncated_dir: str,
                        text: str, prefix: str,
                        max_chars: int) -> tuple[str, str]:
    """Truncate ``text``; when truncated, the FULL content is saved to the
    workspace cache dir and the hint carries its runtime path."""
    text = str(text or "")
    if len(text) <= max_chars:
        return text, ""
    shown = text[:max_chars] + "…"
    path = _save_artifact(executor, truncated_dir, text, prefix)
    if path:
        return shown, (f"\n（内容过长，已截断；完整内容已保存到 {path}，"
                       "可用 workspace_read 读取）")
    return shown, f"\n（内容过长，已截断至 {max_chars} 字符）"


# ── file tools ─────────────────────────────────────────────────────────────

def make_tools(executor: WorkspaceExecutor,
               cfg: WorkspaceToolsConfig) -> list[dict]:
    """Build the workspace file tools (name/category/handler); every file
    operation is one fsops job executed inside the runtime."""
    fs_cfg = {
        "root": executor.default_root,
        "truncated_dir": executor.runtime_path(cfg.truncated_dir)
        if cfg.truncated_dir else "",
        "read_max_bytes": cfg.read_max_bytes,
        "read_max_lines": cfg.read_max_lines,
        "read_warn_bytes": cfg.read_warn_bytes,
        "read_default_line_numbers": cfg.read_default_line_numbers,
        "glob_max_results": cfg.glob_max_results,
        "grep_head_limit": cfg.grep_head_limit,
        "grep_max_columns": cfg.grep_max_columns,
        "edit_max_old_chars": cfg.edit_max_old_chars,
        "edit_output_max_chars": cfg.edit_output_max_chars,
        "write_max_bytes": cfg.write_max_bytes,
        "rm_max_entries": cfg.rm_max_entries,
    }

    def _fs_call(op: str, args: dict) -> tuple[Optional[dict], str]:
        """One fsops job; returns (payload, error_text)."""
        args = dict(args)
        args["cfg"] = fs_cfg
        try:
            payload = executor.fs_op(op, args,
                                     timeout=cfg.fs_timeout_seconds)
        except subprocess.TimeoutExpired:
            return None, f"操作超时（超过 {cfg.fs_timeout_seconds:g} 秒）"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        if not payload.get("ok"):
            return None, str(payload.get("error") or "未知错误")
        return payload, ""

    def read_tool(
        path: str,
        start: int = 1,
        end: int = 0,
        show_line_numbers: Optional[bool] = None,
    ) -> str:
        """读取文件（运行时路径：沙箱容器内路径，可读工作区外如 /tmp）。

        返回 cat -n 风格内容（默认带行号，行号可关闭）。可指定行范围
        （start/end，end=0 表示读到最后）；文件超过配置的字节/行数上限会
        报错而不是截断。"""
        payload, err = _fs_call("read", {
            "path": executor.map_path(path),
            "start": start, "end": end,
            "show_line_numbers": show_line_numbers,
        })
        return f"读取失败: {err}" if err else payload["text"]

    def edit_tool(
        path: str,
        old_string: str,
        new_string: str = "",
        replace_all: bool = False,
    ) -> str:
        """在文件中做精确文本替换（差分修改）。

        返回修改的完整 diff（含被删除行的行号与原文，删除不会被忽略）。
        old_string 必须唯一命中：出现多处且未设置 replace_all 会拒绝。
        new_string 为空表示删除该段文本。"""
        payload, err = _fs_call("edit", {
            "path": executor.map_path(path),
            "old_string": old_string, "new_string": new_string,
            "replace_all": replace_all,
        })
        return f"修改失败: {err}" if err else payload["text"]

    def replace_tool(
        path: str,
        start_line: int,
        end_line: int,
        new_content: str = "",
    ) -> str:
        """将文件第 start_line 行到第 end_line 行（含两端）替换为
        new_content 的内容。

        返回修改的完整 diff（含被删除行的行号与原文）。行号为 1-based；
        new_content 可包含多行；为空表示删除该段行。"""
        payload, err = _fs_call("replace", {
            "path": executor.map_path(path),
            "start_line": start_line, "end_line": end_line,
            "new_content": new_content,
        })
        return f"替换失败: {err}" if err else payload["text"]

    def glob_tool(pattern: str, path: str = "") -> str:
        """按 glob 模式查找文件（支持 ** 递归）。

        返回相对路径列表（节省上下文）；结果数量受配置上限约束，
        超出会截断并提示。path 为搜索起点（运行时路径，默认工作区根）。"""
        payload, err = _fs_call("glob", {
            "pattern": pattern,
            "path": executor.map_path(path) if path else "",
        })
        return f"查找失败: {err}" if err else payload["text"]

    def grep_tool(
        pattern: str,
        path: str = "",
        glob: str = "",
        output_mode: str = "content",
        ignore_case: bool = False,
        context_lines: int = 0,
        hidden: bool = False,
        head_limit: Optional[int] = None,
    ) -> str:
        """按正则搜索文件内容（运行时内执行，纯 Python 无需额外依赖）。

        path 可以是目录（递归搜索）或具体的某个文件（只搜该文件），默认
        工作区根。output_mode：content（默认，带文件名与行号）/
        files_with_matches（只列文件）/ count（统计每文件匹配数）。glob 用于
        过滤文件（如 *.py、*.md）；ignore_case 忽略大小写；context_lines
        附加上下文行；hidden 搜索隐藏文件。二进制文件默认跳过。结果行数受
        head_limit 限制（默认取配置值），超出会截断并提示。"""
        payload, err = _fs_call("grep", {
            "pattern": pattern,
            "path": executor.map_path(path) if path else "",
            "glob": glob, "output_mode": output_mode,
            "ignore_case": ignore_case, "context_lines": context_lines,
            "hidden": hidden, "head_limit": head_limit,
        })
        return f"搜索失败: {err}" if err else payload["text"]

    def append_tool(
        path: str,
        content: str,
    ) -> str:
        """向文件追加内容（不存在则创建）。

        参数:
          path: 运行时文件路径。
          content: 追加的文本内容（支持多行与任意字符，无需 shell 转义）。

        用途: 追加日志/记录/累积数据——比 workspace_exec 用 >> 更稳
        （多行/特殊字符无转义问题）。大小受配置上限约束。
        """
        payload, err = _fs_call("append", {
            "path": executor.map_path(path), "content": content,
        })
        return f"追加失败: {err}" if err else payload["text"]

    def write_tool(
        path: str,
        content: str,
    ) -> str:
        """整文件覆盖写入。

        参数:
          path: 运行时文件路径。
          content: 完整的新内容（覆盖写入，支持多行与任意字符，
            无需 shell 转义）。

        用途: 创建新文件 / 整文件重写。文件已存在时原文件自动备份为
        <path>.old（再写入新内容，提示备份路径）；只保留最近一次备份。
        大小受配置上限约束。
        """
        payload, err = _fs_call("write", {
            "path": executor.map_path(path), "content": content,
        })
        return f"写入失败: {err}" if err else payload["text"]

    # ── workspace_rm：两步确认删除（防误删）────────────────────────────
    _rm_pending: dict[str, dict] = {}
    _rm_lock = threading.Lock()

    def rm_tool(path: str = "", recursive: bool = False,
                confirm: str = "") -> str:
        """删除文件/目录（两步确认，防误删）。

        参数:
          path: 要删除的条目（运行时路径，支持 * 通配符，如
            /home/sandbox/tmp/*）。第一次调用必填；确认删除时不要传。
          recursive: 是否递归删除目录。true=目录连同内容一起删除；
            false=只删文件，匹配到的目录会被跳过并在清单中标注。
          confirm: 第一步返回的 10 位确认 uuid。只传它即执行删除，
            此时不要再传 path。

        用法: 先调用一次得到"将要删除"的清单与 uuid（不会删除任何东西）；
        确认后只传 confirm=uuid 再调一次才真正删除；不想删就忽略。

        返回: 第一步返回清单+uuid+确认提示；第二步返回 uuid+已删除清单
        （清单过长时完整结果落盘为临时文件并返回其路径）。
        """
        confirm_value = str(confirm or "").strip()
        if confirm_value:
            if str(path or "").strip():
                return ("确认失败：确认某一次删除时请不要携带路径信息，"
                        "只需要传递确认的 uuid（本次未执行删除）")
            with _rm_lock:
                pending = _rm_pending.pop(confirm_value, None)
            if pending is None:
                return (f"确认失败：uuid {confirm_value} 不存在或已被使用；"
                        "请重新发起删除请求")
            if time.time() - pending["created_at"] > \
                    float(cfg.rm_confirm_ttl_seconds):
                return (f"确认失败：uuid {confirm_value} 已过期；"
                        "请重新发起删除请求")
            payload, err = _fs_call("rm_delete", {
                "targets": pending["targets"],
                "lines": pending["lines"],
                "recursive": pending["recursive"],
                "confirm": confirm_value,
            })
            if err:
                return f"删除失败：{err}"
            logger.info("workspace_rm deleted uuid=%s targets=%d",
                        confirm_value, len(pending["targets"]))
            return payload["text"]
        # 第一步：只列举，不删除
        raw_path = str(path or "").strip()
        if not raw_path:
            return ("删除失败：第一次调用必须提供 path"
                    f"（如 {executor.default_root}/tmp/*）")
        payload, err = _fs_call("rm_list", {
            "path": executor.map_path(raw_path),
            "recursive": bool(recursive),
        })
        if err:
            return f"删除失败：{err}"
        if payload.get("empty"):
            return payload["text"]
        targets = payload["targets"]
        uuid_value = uuid.uuid4().hex[:10]
        now = time.time()
        with _rm_lock:
            expired = [key for key, value in _rm_pending.items()
                       if now - value["created_at"]
                       > float(cfg.rm_confirm_ttl_seconds)]
            for key in expired:
                _rm_pending.pop(key, None)
            _rm_pending[uuid_value] = {
                "uuid": uuid_value, "targets": targets,
                "recursive": bool(recursive), "created_at": now,
                "lines": payload.get("lines") or {},
            }
        return (payload["text"]
                + "\n请确认是否删除：确认时再次调用 workspace_rm 并且只传 "
                  f'confirm="{uuid_value}"（不要传 path）。'
                + f"\n本次确认 uuid：{uuid_value}"
                + f"（{int(cfg.rm_confirm_ttl_seconds)} 秒内有效）"
                + "\n不删除则忽略本消息即可（不会发生任何删除）。")

    return [
        {"name": _TOOL_PREFIX + "read", "category": _TOOL_CATEGORY + "read",
         "handler": read_tool},
        {"name": _TOOL_PREFIX + "edit", "category": _TOOL_CATEGORY + "edit",
         "handler": edit_tool},
        {"name": _TOOL_PREFIX + "replace",
         "category": _TOOL_CATEGORY + "replace",
         "handler": replace_tool},
        {"name": _TOOL_PREFIX + "append",
         "category": _TOOL_CATEGORY + "append",
         "handler": append_tool},
        {"name": _TOOL_PREFIX + "write", "category": _TOOL_CATEGORY + "write",
         "handler": write_tool},
        {"name": _TOOL_PREFIX + "glob", "category": _TOOL_CATEGORY + "glob",
         "handler": glob_tool},
        {"name": _TOOL_PREFIX + "grep", "category": _TOOL_CATEGORY + "grep",
         "handler": grep_tool},
        {"name": _TOOL_PREFIX + "rm", "category": _TOOL_CATEGORY + "rm",
         "handler": rm_tool},
    ]


# ── registration ───────────────────────────────────────────────────────────

def register_workspace_tools(registry: ToolRegistry,
                             executor: WorkspaceExecutor,
                             cfg: WorkspaceToolsConfig) -> None:
    """Register the workspace tools: exec + exec_async + the file tools.
    Schema is generated from the handler signature + docstring."""

    def exec_tool(
        cmd: str,
        timeout: Optional[float] = None,
        stdin: str = "",
        workdir: str = "",
    ) -> str:
        """在工作区运行时执行命令（bash，前台阻塞，只适合短命令）。

        timeout: 本次最多等待的秒数；不传用配置默认值。**实际等待 =
        min(timeout, exec_max_timeout_seconds 配置上限)——传再大也会被
        截断**：前台 exec 的本质是阻塞等待，只应等一小会儿，不要用它跑
        长任务，也不要指望设置大 timeout 来延长等待。workdir 默认工作区
        根目录（运行时路径，沙箱模式下为容器内路径）。超时会被中断并
        提示转 workspace_exec_async（后台执行）。返回 returncode +
        stdout + stderr。"""
        requested = cfg.exec_default_timeout_seconds if timeout is None \
            else float(timeout)
        effective = min(requested, cfg.exec_max_timeout_seconds)
        try:
            result = executor.exec(
                cmd, timeout=effective, stdin=stdin or None, workdir=workdir)
            stdout, stdout_hint = _truncate_with_save(
                executor, cfg.truncated_dir, result.stdout, "exec_stdout",
                cfg.exec_output_max_chars)
            stderr, stderr_hint = _truncate_with_save(
                executor, cfg.truncated_dir, result.stderr, "exec_stderr",
                cfg.exec_output_max_chars)
            return (f"returncode={result.returncode}\n"
                    f"stdout:\n{stdout}{stdout_hint}\n"
                    f"stderr:\n{stderr}{stderr_hint}")
        except subprocess.TimeoutExpired:
            return (f"执行超时：命令未在 {effective:g} 秒内完成"
                    f"（前台等待上限 = min(timeout, "
                    f"{cfg.exec_max_timeout_seconds:g} 秒配置上限)）。\n"
                    f"长任务请使用 workspace_exec_async（后台执行，"
                    f"上限 {cfg.exec_async_timeout_seconds:g} 秒）")
        except Exception as exc:  # noqa: BLE001
            return f"执行失败: {exc}"

    def exec_async_tool(
        cmd: str,
        timeout: Optional[float] = None,
        stdin: str = "",
        workdir: str = "",
    ) -> str:
        """在工作区运行时异步执行命令（后台，限时，适合长任务）。

        立即返回任务 ID；命令由后台 worker 线程执行，**实际上限 =
        min(timeout, exec_async_timeout_seconds 配置上限)**（不传 timeout
        则用配置值）。**任务完成（含超时/失败）后系统会主动把结果作为
        通知推送给 agent，无需轮询**；结果同时写入日志文件 <task_id>.json
        （含 returncode/stdout/stderr/耗时），如需查看细节可用
        workspace_read 读取。"""
        requested = cfg.exec_async_timeout_seconds if timeout is None \
            else float(timeout)
        effective = min(requested, cfg.exec_async_timeout_seconds)
        try:
            task_id = executor.submit_async(
                cmd, timeout=effective, stdin=stdin or None, workdir=workdir)
            runtime_log = os.path.join(
                executor.container_async_log_dir, f"{task_id}.json")
            host_log = os.path.join(executor.async_log_dir, f"{task_id}.json")
            if executor.mode == "sandbox":
                return (f"已提交后台任务 task_id={task_id}\n"
                        f"完成后系统会自动通知结果，无需轮询。\n"
                        f"结果文件（容器内可读）: {runtime_log}\n"
                        f"结果文件（宿主侧）: {host_log}\n"
                        f"（超时上限默认 "
                        f"{cfg.exec_async_timeout_seconds:g} 秒）")
            return (f"已提交后台任务 task_id={task_id}\n"
                    f"完成后系统会自动通知结果，无需轮询。\n"
                    f"结果文件: {runtime_log}\n"
                    f"（超时上限默认 "
                    f"{cfg.exec_async_timeout_seconds:g} 秒）")
        except Exception as exc:  # noqa: BLE001
            return f"提交失败: {exc}"

    registry.register("workspace_exec", exec_tool)
    registry.register("workspace_exec_async", exec_async_tool)
    for tool in make_tools(executor, cfg):
        registry.register(tool["name"], tool["handler"])
