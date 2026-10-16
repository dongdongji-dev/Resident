"""Workspace path mapping — the ONLY entry points for host <-> container
path conversion, with anti-escape.

One workspace maps a host directory (``workspace.host_root``) to the Agent's
user directory inside the container (``workspace.container_path``). These two
functions centralize every conversion:

- ``to_host(container_path)``     containment check on the CONTAINER side is
  lexical (we cannot resolve the container filesystem from the host); then
  the resulting host path is additionally verified with ``realpath`` so a
  host-side symlink cannot escape the workspace either.
- ``to_container(host_path)``     the host path is resolved with
  ``realpath`` (catches ``..`` and host symlinks), converted to a containment
  container path.

Any path that points outside the workspace raises ``PathEscapeError``.
"""

from __future__ import annotations

import errno
import os
import posixpath
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.paths")


class PathEscapeError(ValueError):
    """Raised when a path conversion would escape the workspace root."""


def _is_within(normalized: str, root: str) -> bool:
    return (normalized == root
            or normalized.startswith(root.rstrip("/") + "/"))


def _realpath_within(path: str, root: str) -> bool:
    """True when ``path`` resolves (following symlinks for existing parts)
    strictly inside ``root``. Safe for not-yet-existing paths: realpath
    resolves the existing prefix and appends the remaining components."""
    resolved = os.path.realpath(os.path.abspath(str(path)))
    root_real = os.path.realpath(os.path.abspath(str(root)))
    return (resolved == root_real
            or resolved.startswith(root_real + os.sep))


def is_inside_workspace(path: str, host_root: str) -> bool:
    """内核托管写入前的包含性校验（跟随符号链接）：目标真实路径必须仍在
    工作区根内——防沙箱把托管目录/文件换成符号链接，把内核更高权限的写
    带出工作区（.truncated / .tasks_log / .resident / memory / download 等）。"""
    return _realpath_within(path, host_root)


_UNIX_SAFE_OPEN = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


def _open_dir_chain(dir_path: str, root: str, create: bool) -> int:
    """逐段 O_NOFOLLOW 解析并打开 root 内的目录链，返回目录 fd。

    "打开即校验"：每一步都踩在上一步验证过的真实目录 fd 上，任何一段命中
    符号链接/非常规目录立刻失败——关闭"检查后再按路径重走一遍"的 TOCTOU
    窗口。root 自身按信任处理（配置值），只对其内部相对段做严格校验。"""
    root_abs = os.path.abspath(str(root))
    dir_abs = os.path.abspath(str(dir_path))
    try:
        rel = os.path.relpath(dir_abs, root_abs)
    except ValueError as exc:  # 跨盘符（Windows）
        raise PathEscapeError(f"目录不在工作区内: {dir_path!r}") from exc
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise PathEscapeError(f"目录不在工作区内: {dir_path!r}")
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if create:
        # 保持 makedirs 语义：允许连工作区根一起补建（根为信任的配置路径）
        try:
            os.makedirs(root_abs, exist_ok=True)
        except OSError as exc:
            logger.warning("workspace root create failed: %s", exc)
    fd = os.open(root_abs, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts:
            if create:
                try:
                    os.mkdir(comp, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(
                    comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PathEscapeError(
                        f"路径包含符号链接或非常规目录: {dir_path!r}"
                    ) from exc
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file_inside(host_path: str, host_root: str, mode: str = "rb",
                     create_parents: bool = False,
                     encoding: Optional[str] = None) -> Any:
    """在 host_root 内安全打开文件（r/rb/w/a），返回普通文件对象。

    逐段 O_NOFOLLOW + 目录 fd 递进：校验与打开是同一条 fd 链上的原子过程，
    路径中任何一段或目标本身是符号链接都立即失败（PathEscapeError），不存在
    "检查之后按字符串路径重新走一遍"的窗口。无 dir_fd 的平台（Windows）退回
    realpath 校验 + 普通 open。"""
    host_abs = os.path.abspath(str(host_path))
    if not _UNIX_SAFE_OPEN:
        if not _realpath_within(host_abs, host_root):
            logger.warning("open refused (escape): %s", host_abs)
            raise PathEscapeError(f"目标不在工作区内: {host_abs!r}")
        return open(host_abs, mode, encoding=encoding)
    flags = {
        "r": os.O_RDONLY,
        "rb": os.O_RDONLY,
        "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    }.get(mode)
    if flags is None:
        raise ValueError(f"unsupported mode: {mode!r}")
    directory, name = os.path.split(host_abs)
    dir_fd = _open_dir_chain(directory, host_root, create=create_parents)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            logger.warning("open refused (symlink): %s", host_abs)
            raise PathEscapeError(
                f"目标包含符号链接: {host_abs!r}") from exc
        raise
    finally:
        os.close(dir_fd)
    return os.fdopen(fd, mode, encoding=encoding)


def replace_text_inside(host_path: str, text: str, host_root: str,
                        create_parents: bool = False) -> None:
    """同目录临时文件 + 原子改名写入（全程 fd 相对）：防撕裂写入；目标
    被换成符号链接时，改名只替换链接本身、绝不跟随写入。"""
    host_abs = os.path.abspath(str(host_path))
    if not _UNIX_SAFE_OPEN:
        if not _realpath_within(host_abs, host_root):
            logger.warning("replace refused (escape): %s", host_abs)
            raise PathEscapeError(f"目标不在工作区内: {host_abs!r}")
        tmp = host_abs + ".tmp"
        Path(tmp).write_text(str(text), encoding="utf-8")
        os.replace(tmp, host_abs)
        return
    directory, name = os.path.split(host_abs)
    dir_fd = _open_dir_chain(directory, host_root, create=create_parents)
    try:
        tmp_name = "." + name + ".tmp"
        fd = os.open(tmp_name,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(text))
        os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


@dataclass(frozen=True, slots=True)
class WorkspacePath:
    host_root: str
    container_root: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "host_root",
            os.path.normpath(os.path.abspath(str(self.host_root))))
        object.__setattr__(
            self, "container_root", str(self.container_root).rstrip("/"))
        if not self.host_root or not self.container_root:
            raise ValueError("workspace host_root/container_root required")

    # ── conversions ─────────────────────────────────────────────────────

    def to_host(self, container_path: str) -> str:
        """Container-side (relative or absolute) path -> host absolute path.
        Raises ``PathEscapeError`` on any escape."""
        raw = str(container_path).replace("\\", "/")
        if not raw.startswith("/"):
            raw = self.container_root + "/" + raw
        normalized = posixpath.normpath(raw)
        if not _is_within(normalized, self.container_root):
            raise PathEscapeError(
                f"container path escapes workspace "
                f"{self.container_root!r}: {container_path!r}")
        rel = normalized[len(self.container_root):].strip("/")
        host = (os.path.join(self.host_root, *rel.split("/"))
                if rel else self.host_root)
        # host-side guard: realpath must stay inside the workspace (symlinks)
        if not _realpath_within(host, self.host_root):
            raise PathEscapeError(
                f"host path resolves outside workspace "
                f"{self.host_root!r}: {container_path!r}")
        return os.path.normpath(host)

    def to_container(self, host_path: str) -> str:
        """Host path -> container-side path. Raises ``PathEscapeError`` on
        any escape (``..`` or host-side symlinks out of the workspace)."""
        normalized = os.path.abspath(str(host_path))
        if not _realpath_within(normalized, self.host_root):
            raise PathEscapeError(
                f"host path escapes workspace {self.host_root!r}: "
                f"{host_path!r}")
        rel = os.path.relpath(normalized, self.host_root)
        if rel == ".":
            return self.container_root
        # 容器侧路径一律 posix：Windows 宿主上 os.path.join 会带反斜杠，
        # 容器里 "\" 是合法文件名字符，会导致 docker exec 找不到文件
        rel = rel.replace(os.sep, "/")
        if os.altsep:
            rel = rel.replace(os.altsep, "/")
        return self.container_root + "/" + rel

    # ── helpers ─────────────────────────────────────────────────────────

    @property
    def host_workspace(self) -> str:
        return self.host_root

    @property
    def container_workspace(self) -> str:
        return self.container_root


def save_cutoff(workspace: "WorkspacePath", text: str, prefix: str,
                truncated_dir: str,
                expose: Optional[Callable[[str], str]] = None
                ) -> Optional[str]:
    """Persist full (would-be-truncated-away) content into the workspace
    cache dir; returns the runtime path the agent can use (None on failure).
    Nothing is discarded without a trace. ``expose`` is the executor's
    unified ``runtime_path`` resolver (sandbox probe / host bash); without
    it the lexical container mapping is used."""
    if not text or not truncated_dir:
        return None
    try:
        directory = os.path.abspath(truncated_dir)
        filename = (f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}.txt")
        host = os.path.join(directory, filename)
        with open_file_inside(host, workspace.host_root, "w",
                              create_parents=True,
                              encoding="utf-8") as handle:
            handle.write(str(text))
        if expose is not None:
            try:
                return expose(host)
            except Exception:  # noqa: BLE001 — 解析失败退回词法映射
                pass
        return workspace.to_container(host)
    except Exception:  # noqa: BLE001
        return None
