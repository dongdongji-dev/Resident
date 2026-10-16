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

import os
import posixpath
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
        os.makedirs(directory, exist_ok=True)
        filename = (f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}.txt")
        host = os.path.join(directory, filename)
        Path(host).write_text(str(text), encoding="utf-8")
        if expose is not None:
            try:
                return expose(host)
            except Exception:  # noqa: BLE001 — 解析失败退回词法映射
                pass
        return workspace.to_container(host)
    except Exception:  # noqa: BLE001
        return None
