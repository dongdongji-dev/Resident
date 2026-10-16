"""Workspace executor layer — one abstraction over two runtimes.

The Agent's workspace capability (shell commands + file operations) always
executes INSIDE a runtime, chosen by ``workspace_tools.mode``:

- ``SandboxExecutor`` (``mode: sandbox``, production): every command goes
  through ``docker exec`` into the workspace container (``/home/sandbox`` is
  bind-mounted from ``data/workspace``). Isolation comes from the container
  boundary, so paths are handed to the runtime verbatim — no host/container
  mapping, no escape checks: container paths like ``/tmp/x`` just work.
- ``HostExecutor`` (``mode: host``): commands run via the host system bash
  and file operations via the host interpreter. Host mode is an early
  scaffold — its security limits (sandboxing the host runtime) are future
  work; today paths are only mapped (container root -> host root), not
  restricted.

Capabilities (both modes):

- ``exec`` — one bounded foreground shell command;
- ``submit_async`` — bounded background task on a worker pool, result JSON
  persisted under ``workspace_tools.exec_async_log_dir``;
- ``fs_op`` — one ``fsops`` job (read/edit/glob/grep/rm ...) executed by the
  runtime's python (sandbox: the shipped ``<workspace>/.resident/fsops.py``;
  host: this package's ``fsops.py``), driven by a JSON job on stdin.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 后台任务完成通知的固定前缀（runtime_block 注入说明共用）
BACKGROUND_DONE_PREFIX = "[后台任务完成 #"

from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.paths import WorkspacePath
from kernel.agent_framework.timeutil import now_str
from kernel.osdb.notice import NoticeStore

logger = get_logger("kernel.agent_framework.sandbox")

# fsops 脚本在运行时内的落点：沙箱走 workspace 绑定的隐藏目录
# （重建容器/重启内核后仍在，且重启内核时会校验并刷新），宿主模式直接用包内文件。
_RUNTIME_DIR = ".resident"
_FSOPS_NAME = "fsops.py"


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Exec-related values (subset of the ``workspace_tools`` YAML section)."""
    container: str
    exec_timeout_seconds: float
    async_workers: int
    async_timeout_seconds: float
    async_log_dir: str


class WorkspaceExecutor:
    """Base executor: bounded commands, async pool, fsops RPC.

    Subclasses provide the runtime mechanics (``_run`` / ``_run_fsops``) and
    path semantics (``map_path`` / ``runtime_path`` / ``default_root``).
    """

    mode = "?"

    def __init__(self, config: SandboxConfig, workspace: WorkspacePath,
                 notices: Optional[NoticeStore] = None) -> None:
        self._config = config
        self._workspace = workspace
        self._notices = notices  # exec_async 完成通知走 notice 通道（beat 统一推送）
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_lock = threading.Lock()
        # 结果文件必须落进工作区挂载内（agent 才能读到）。宿主侧路径用于写
        # 文件；运行时侧路径返回给 agent 查询。
        self._async_log_dir = os.path.abspath(config.async_log_dir)

    @property
    def workspace(self) -> WorkspacePath:
        return self._workspace

    @property
    def async_log_dir(self) -> str:
        """Host-side result directory (where the worker writes files)."""
        return self._async_log_dir

    @property
    def container_async_log_dir(self) -> str:
        """Runtime-side result directory (the path the agent can read)."""
        raise NotImplementedError

    @property
    def default_root(self) -> str:
        """Runtime path of the workspace root (default workdir/base)."""
        raise NotImplementedError

    def map_path(self, path: str) -> str:
        """Agent-supplied path -> this runtime's path (identity in sandbox;
        container-root rewrite in host mode)."""
        raise NotImplementedError

    def runtime_path(self, host_path: str) -> str:
        """UNIFIED host-artifact -> runtime path resolution (the ONLY way
        kernel-written files are described to the agent):

        - sandbox: the bind-mount mapping, normalized by ``realpath`` EXECUTED
          INSIDE the container (authoritative for what the agent sees);
        - host: ``realpath`` executed via the system bash.

        Any probe failure falls back to the lexical mapping — the returned
        path is always usable in the current runtime."""
        raise NotImplementedError

    def _pool_get(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self._config.async_workers,
                    thread_name_prefix="sandbox-task")
            return self._pool

    # ── foreground exec ────────────────────────────────────────────────

    def exec(self, cmd: str, timeout: Optional[float] = None,
             stdin: Optional[str] = None,
             workdir: str = "") -> subprocess.CompletedProcess:
        """Run ``cmd`` via bash in the runtime (foreground).

        Bounded by ``timeout`` (default ``exec_timeout_seconds``). stdin is
        fed via the pipe when provided; otherwise the child gets DEVNULL so
        a command reading stdin returns EOF immediately instead of hanging.
        """
        timeout = self._config.exec_timeout_seconds \
            if timeout is None else float(timeout)
        workdir = self.map_path(workdir) if workdir else self.default_root
        logger.debug("sandbox exec timeout=%s workdir=%s cmd=%.200s",
                     timeout, workdir, cmd)
        return self._run(cmd, timeout, stdin, workdir)

    def _run(self, cmd: str, timeout: float, stdin: Optional[str],
             workdir: str) -> subprocess.CompletedProcess:
        raise NotImplementedError

    # ── fs ops (fsops JSON job on stdin) ───────────────────────────────

    def fs_op(self, op: str, args: dict,
              timeout: Optional[float] = None) -> dict:
        """Execute one fsops job in the runtime; returns its parsed payload
        (``{"ok": true/false, ...}``). Raises on transport errors only."""
        job = json.dumps({"op": op, "args": args}, ensure_ascii=False)
        timeout = self._config.exec_timeout_seconds \
            if timeout is None else float(timeout)
        proc = self._run_fsops(job, timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"{op} 在运行时执行失败（exit={proc.returncode}）: "
                               f"{detail[-500:]}")
        try:
            payload = json.loads(proc.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"{op} 返回无法解析: {proc.stdout[:300]!r}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{op} 返回格式错误")
        return payload

    def _run_fsops(self, job: str, timeout: float) -> subprocess.CompletedProcess:
        raise NotImplementedError

    # ── bounded background tasks ───────────────────────────────────────

    def submit_async(self, cmd: str, timeout: Optional[float] = None,
                     stdin: Optional[str] = None, workdir: str = "") -> str:
        """Queue a bounded background task; returns its task_id immediately.

        The worker runs ``cmd`` with ``timeout`` (default
        ``async_timeout_seconds``) and persists the outcome
        (``done``/``timeout``/``error`` + stdout/stderr + elapsed) as JSON at
        ``<async_log_dir>/<task_id>.json``.
        """
        task_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        timeout = self._config.async_timeout_seconds \
            if timeout is None else float(timeout)
        workdir = self.map_path(workdir) if workdir else self.default_root
        os.makedirs(self._config.async_log_dir, exist_ok=True)
        self._pool_get().submit(
            self._run_task, task_id, cmd, timeout, stdin, workdir)
        logger.info("sandbox async submitted task_id=%s cmd=%.120s",
                    task_id, cmd)
        return task_id

    def _run_task(self, task_id: str, cmd: str, timeout: float,
                  stdin: Optional[str], workdir: str) -> None:
        started = time.monotonic()
        record: dict = {
            "task_id": task_id,
            "cmd": cmd,
            "workdir": workdir,
            "timeout": timeout,
            "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            result = self.exec(cmd, timeout=timeout, stdin=stdin,
                               workdir=workdir)
            record.update(status="done", returncode=result.returncode,
                          stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired:
            record.update(status="timeout",
                          error=f"超过时限 {timeout:g} 秒")
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox task %s error: %s", task_id, exc)
            record.update(status="error", error=str(exc))
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            record["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write_log(task_id, record)
            # 完成通知：创建 notice，由 beat 统一推送给 agent
            if self._notices is not None:
                self._notify_completed(record)

    def _notify_completed(self, record: dict) -> None:
        try:
            lines = [
                f"{BACKGROUND_DONE_PREFIX}{record['task_id']}]",
                f"命令: {record.get('cmd', '')}",
                f"状态: {record.get('status', 'done')}",
                f"耗时: {record.get('elapsed_seconds', 0):g} 秒",
            ]
            if record.get("status") == "done":
                stdout = str(record.get("stdout", "")).strip()
                if stdout:
                    lines.append(f"输出: {stdout[:200]}")
            else:
                lines.append(f"错误: {record.get('error', '')}")
            lines.append(
                f"结果文件: "
                f"{self.container_async_log_dir}/{record['task_id']}.json")
            self._notices.create(
                content="\n".join(lines), kind="once", due_at=now_str())
            logger.info("exec_async notice created task_id=%s status=%s",
                        record.get("task_id"), record.get("status"))
        except Exception as exc:  # noqa: BLE001 — 通知失败不影响任务本身
            logger.warning("exec_async notify failed: %s", exc)

    def _write_log(self, task_id: str, record: dict) -> None:
        path = os.path.join(self._config.async_log_dir, f"{task_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("sandbox task log write failed: %s", exc)

    def shutdown(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


class SandboxExecutor(WorkspaceExecutor):
    """Run everything inside the workspace container (``docker exec``)."""

    mode = "sandbox"

    def __init__(self, config: SandboxConfig, workspace: WorkspacePath,
                 notices: Optional[NoticeStore] = None) -> None:
        if not config.container:
            raise ValueError("workspace_tools.container must not be empty")
        super().__init__(config, workspace, notices)
        try:
            self._container_async_log_dir = workspace.to_container(
                self._async_log_dir)
        except ValueError as exc:
            raise ValueError(
                f"exec_async_log_dir 必须在 workspace.host_root 内: "
                f"{config.async_log_dir!r}") from exc
        # fsops 脚本：内核侧源文件 -> workspace 隐藏目录（容器内可读）
        self._fsops_host = os.path.join(
            workspace.host_root, _RUNTIME_DIR, _FSOPS_NAME)
        self._fsops_container = workspace.to_container(self._fsops_host)
        self._fsops_source = Path(__file__).with_name(_FSOPS_NAME).read_text(
            encoding="utf-8")
        self._sync_fsops()

    @property
    def container_async_log_dir(self) -> str:
        return self._container_async_log_dir

    @property
    def default_root(self) -> str:
        return self._workspace.container_root

    def map_path(self, path: str) -> str:
        """Sandbox mode: paths are container paths, handed over verbatim —
        the container boundary is the isolation, no checks needed."""
        return str(path)

    def runtime_path(self, host_path: str) -> str:
        mapped = self._workspace.to_container(host_path)
        try:
            proc = self._run("realpath -m -- " + shlex.quote(mapped),
                             timeout=min(10.0,
                                         self._config.exec_timeout_seconds),
                             stdin=None, workdir=self.default_root)
            lines = [line.strip() for line in
                     (proc.stdout or "").splitlines() if line.strip()]
            if proc.returncode == 0 and lines:
                return lines[-1]
        except Exception:  # noqa: BLE001 — 探测失败退回词法映射
            pass
        return mapped

    def _sync_fsops(self) -> None:
        """Keep the runtime copy of fsops.py in sync with this package's
        source (cheap re-check each startup / first use after edits)."""
        try:
            current = Path(self._fsops_host).read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == self._fsops_source:
            return
        try:
            os.makedirs(os.path.dirname(self._fsops_host), exist_ok=True)
            tmp = self._fsops_host + ".tmp"
            Path(tmp).write_text(self._fsops_source, encoding="utf-8")
            os.replace(tmp, self._fsops_host)
            logger.info("fsops synced to %s", self._fsops_container)
        except OSError as exc:
            logger.warning("fsops sync failed: %s", exc)

    def _run(self, cmd: str, timeout: float, stdin: Optional[str],
             workdir: str) -> subprocess.CompletedProcess:
        argv = ["docker", "exec", "-i", "-w", workdir,
                self._config.container, "bash", "-c", cmd]
        kwargs: dict = dict(
            timeout=timeout,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
        return subprocess.run(argv, capture_output=True, encoding="utf-8",
                              errors="replace", **kwargs)

    def _run_fsops(self, job: str, timeout: float) -> subprocess.CompletedProcess:
        self._sync_fsops()
        argv = ["docker", "exec", "-i",
                "-e", "PYTHONUTF8=1",
                "-w", self.default_root,
                self._config.container, "python3", self._fsops_container]
        return subprocess.run(argv, capture_output=True, encoding="utf-8",
                              errors="replace", input=job, timeout=timeout)


class HostExecutor(WorkspaceExecutor):
    """Early host-mode scaffold: system bash + host interpreter.

    Security limits for this mode are future work; paths are only mapped
    (workspace container root -> host root), not restricted.
    """

    mode = "host"

    def __init__(self, config: SandboxConfig, workspace: WorkspacePath,
                 notices: Optional[NoticeStore] = None) -> None:
        super().__init__(config, workspace, notices)
        self._fsops_host = str(Path(__file__).with_name(_FSOPS_NAME))

    @property
    def container_async_log_dir(self) -> str:
        return self._async_log_dir

    @property
    def default_root(self) -> str:
        return self._workspace.host_root

    def map_path(self, path: str) -> str:
        raw = str(path)
        root = self._workspace.container_root
        if raw == root:
            return self._workspace.host_root
        prefix = root + "/"
        if raw.startswith(prefix):
            return os.path.join(self._workspace.host_root,
                                raw[len(prefix):])
        return raw

    def runtime_path(self, host_path: str) -> str:
        value = os.path.abspath(str(host_path))
        try:
            proc = self._run("realpath -- " + shlex.quote(value),
                             timeout=min(10.0,
                                         self._config.exec_timeout_seconds),
                             stdin=None, workdir=self.default_root)
            lines = [line.strip() for line in
                     (proc.stdout or "").splitlines() if line.strip()]
            if proc.returncode == 0 and lines:
                return lines[-1]
        except Exception:  # noqa: BLE001 — 探测失败退回本机 realpath
            pass
        return os.path.realpath(value)

    def _run(self, cmd: str, timeout: float, stdin: Optional[str],
             workdir: str) -> subprocess.CompletedProcess:
        kwargs: dict = dict(
            timeout=timeout,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
        return subprocess.run(["bash", "-c", cmd], cwd=workdir,
                              capture_output=True, encoding="utf-8",
                              errors="replace", **kwargs)

    def _run_fsops(self, job: str, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, self._fsops_host],
                              cwd=self.default_root, capture_output=True,
                              encoding="utf-8", errors="replace", input=job,
                              timeout=timeout)


def build_executor(mode: str, config: SandboxConfig, workspace: WorkspacePath,
                   notices: Optional[NoticeStore] = None) -> WorkspaceExecutor:
    """Executor factory by ``workspace_tools.mode``."""
    if mode == "sandbox":
        return SandboxExecutor(config, workspace, notices)
    if mode == "host":
        logger.warning("workspace tools in HOST mode — security limits are "
                       "not implemented yet; only use on trusted setups")
        return HostExecutor(config, workspace, notices)
    raise ValueError(f"未知 workspace_tools.mode: {mode!r}（sandbox|host）")
