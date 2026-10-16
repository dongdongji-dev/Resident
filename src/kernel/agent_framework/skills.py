"""Skill system: discoverable
``SKILL.md`` packages under ``<skills_dir>/<name>/SKILL.md``.

- ``scan_skills`` / ``skills_prompt``: whenever the system prompt is
  assembled (re-rendered per round from the live tool registry), list every
  skill's directory name + front matter (name/description) and append them
  to the system prompt (full bodies stay OUT of the prompt - loaded on
  demand; new skills therefore need no restart);
- ``load_skill`` / ``make_load_skills``: the ``skills_load`` tool returns
  the COMPLETE SKILL.md bodies for the requested skill names (matched by
  directory name).

The skills directory lives on the HOST (``data/workspace/.skills``); the
scan is an internal mechanism, the tool returns content only - the agent
never sees host paths.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.skills")


def _parse_front_matter(text: str) -> dict:
    """Parse the leading ``---\nname: ..\ndescription: ..\n---`` block."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict = {}
    for line in text[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def scan_skills(skills_dir: str) -> list[dict]:
    """Scan ``<skills_dir>/*/SKILL.md``; returns
    ``[{name, path, meta: {name, description}}]`` (safe: first-level
    subdirectories only)."""
    skills: list[dict] = []
    try:
        if not os.path.isdir(skills_dir):
            return []
        for entry in sorted(os.listdir(skills_dir)):
            name = entry.strip("/")
            if not name or name.startswith(".") or "/" in name or "\\" in name:
                continue
            skill_md = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            try:
                with open(skill_md, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            skills.append({"name": name, "path": skill_md,
                           "meta": _parse_front_matter(text)})
    except OSError as exc:
        logger.warning("skill scan failed: %s", exc)
    return skills


def skills_prompt(skills_dir: str) -> str:
    """System-prompt fragment listing available skills (directory name +
    front matter only - the full bodies are loaded on demand)."""
    skills = scan_skills(skills_dir)
    if not skills:
        return ""
    lines = ["## 可用技能", "",
             "技能可通过 skills_load 工具加载完整说明。技能目录："]
    for skill in skills:
        meta = skill["meta"]
        block = [f"skill 目录：{skill['name']}/", "---"]
        if meta.get("name"):
            block.append(f"name: {meta['name']}")
        if meta.get("description"):
            block.append(f"description: \"{meta['description']}\"")
        block.append("---")
        lines.append("\n".join(block))
    return "\n".join(lines)


def load_skill(skills_dir: str, name: str) -> Optional[str]:
    """Full SKILL.md body for a skill directory name (None when missing)."""
    name = str(name or "").strip()
    if (not name or name.startswith(("/", "."))
            or ".." in name.split("/") or "/" in name or "\\" in name):
        return None
    path = os.path.join(skills_dir, name, "SKILL.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def make_load_skills(skills_dir: str) -> Callable[..., Any]:
    """Build the ``skills_load`` tool: return the COMPLETE SKILL.md
    documents for the requested skill names (directory names)."""

    async def skills_load(skill_names: list) -> str:
        """加载一个或多个技能的完整 SKILL.md 说明（按技能目录名）。

        系统提示中只列出技能清单；需要实际执行某个技能时先用本工具
        加载它的完整说明。"""
        if isinstance(skill_names, str):
            try:
                skill_names = json.loads(skill_names)
            except (TypeError, ValueError):
                skill_names = [skill_names]
        if not isinstance(skill_names, list) or not skill_names:
            return "skill_names 必须是非空列表（技能目录名）"
        parts: list = []
        for name in skill_names:
            name = str(name or "").strip()
            if (not name or name.startswith(("/", "."))
                    or ".." in name.split("/") or "/" in name or "\\" in name):
                return f"非法技能名: {name!r}"
            body = load_skill(skills_dir, name)
            if body is None:
                return f"技能不存在: {name!r}"
            parts.append(f"### 技能 {name}\n\n{body.strip()}")
        return "\n\n".join(parts)

    return skills_load

# ── 管理界面（前端「技能」视图）：列表 / 详情 / 安装(zip) / 回收删除 ──────

_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def sanitize_skill_name(raw: str) -> str:
    """合法 skill 目录名，否则返回 ''（禁路径分隔符/前导点/超长）。"""
    name = str(raw or "").strip()
    return name if _SKILL_NAME_RE.match(name) else ""


def list_skills(skills_dir: str) -> list[dict[str, Any]]:
    """管理界面清单：目录名 + front matter 标题/描述 + 文件数/字节/更新时间。"""
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        files = 0
        total = 0
        newest = 0.0
        for path in entry.rglob("*"):
            try:
                if path.is_file():
                    stat = path.stat()
                    files += 1
                    total += stat.st_size
                    newest = max(newest, stat.st_mtime)
            except OSError:
                continue
        meta: dict = {}
        skill_md = entry / "SKILL.md"
        try:
            meta = _parse_front_matter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            pass
        try:
            newest = max(newest, entry.stat().st_mtime)
        except OSError:
            pass
        result.append({
            "name": entry.name,
            "title": str(meta.get("name", "")),
            "description": str(meta.get("description", "")),
            "has_skill_md": skill_md.is_file(),
            "files": files,
            "bytes": total,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(newest)),
        })
    return result


def skill_detail(skills_dir: str, name: str, max_chars: int = 0) -> Optional[str]:
    """SKILL.md 全文（预览用；超长截断标注）。非法名/不存在返回 None。"""
    safe = sanitize_skill_name(name)
    if not safe:
        return None
    path = Path(skills_dir) / safe / "SKILL.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，原 {len(text)} 字符）"
    return text


def _validate_zip_entries(zf: Any, max_total_bytes: int,
                          max_files: int) -> list[Any]:
    """校验条目：防 zip slip/符号链接/超限；返回要解压的文件条目。"""
    infos: list[Any] = []
    total = 0
    for info in zf.infolist():
        raw = str(info.filename or "")
        if not raw or raw.endswith("/"):
            continue  # 目录条目（文件路径会隐式建目录）
        name = raw.replace("\\", "/")
        if name.startswith("/") or ":" in name.split("/")[0][:2]:
            raise ValueError(f"压缩包包含绝对路径，已拒绝: {raw}")
        parts = name.split("/")
        if any(part in ("..", "") for part in parts) or "\x00" in name:
            raise ValueError(f"压缩包包含非法路径，已拒绝: {raw}")
        if (name.startswith("__MACOSX/") or name.endswith(".DS_Store")):
            continue  # macOS 打包垃圾，跳过
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"压缩包包含符号链接，已拒绝: {raw}")
        total += int(info.file_size or 0)
        if max_total_bytes > 0 and total > max_total_bytes:
            raise ValueError(
                f"解压后总大小超过上限 {max_total_bytes} 字节")
        infos.append(info)
        if max_files > 0 and len(infos) > max_files:
            raise ValueError(f"文件数超过上限 {max_files}")
    return infos


def _extract_zip(zf: Any, infos: list[Any], dest: Path,
                 max_total_bytes: int) -> tuple[int, int]:
    """手动解压到 dest（实际写入字节也计数，防头部造假）。"""
    files = 0
    total = 0
    for info in infos:
        relative = str(info.filename).replace("\\", "/")
        target = dest.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, open(target, "wb") as out:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if max_total_bytes > 0 and total > max_total_bytes:
                    raise ValueError(
                        f"解压后总大小超过上限 {max_total_bytes} 字节")
                out.write(chunk)
        files += 1
    return files, total


def _find_skill_root(tmp: Path) -> Optional[Path]:
    """skill 根：根目录直接有 SKILL.md，或唯一顶层目录里有。"""
    if (tmp / "SKILL.md").is_file():
        return tmp
    children = [p for p in tmp.iterdir()
                if not p.name.startswith(".") and p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir() \
            and (children[0] / "SKILL.md").is_file():
        return children[0]
    return None


def _move_away(source: Path, dest_dir: Path, name: str) -> str:
    """把目录移动到归档目录（覆盖备份/回收共用），返回归档路径。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{name}-{stamp}-{uuid.uuid4().hex[:6]}"
    shutil.move(str(source), str(dest))
    return str(dest)


def install_skill_zip(data: bytes, *, skills_dir: str, backup_dir: str,
                      fallback_name: str = "",
                      max_zip_bytes: int = 0, max_total_bytes: int = 0,
                      max_files: int = 0) -> dict[str, Any]:
    """安装一个 zip 形式的 skill：校验 → 解压临时目录 → 定名 → 原子落位。

    名称优先取 SKILL.md front matter 的 ``name``；其次唯一顶层目录名；
    再次上传文件名（去扩展名）。同名时旧版备份到 ``backup_dir``。
    返回 {name, files, bytes, backup}；失败抛 ValueError（中文原因）。
    """
    if not data:
        raise ValueError("空文件")
    if max_zip_bytes > 0 and len(data) > max_zip_bytes:
        raise ValueError(f"压缩包 {len(data)} 字节超过上限 {max_zip_bytes}")
    if not data.startswith(b"PK"):
        raise ValueError("不是有效的 zip 压缩包")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"zip 解析失败: {exc}") from exc
    with zf:
        infos = _validate_zip_entries(zf, max_total_bytes, max_files)
        if not infos:
            raise ValueError("压缩包为空")
        tmp = Path(tempfile.mkdtemp(prefix="resident-skill-"))
        try:
            files, total = _extract_zip(zf, infos, tmp, max_total_bytes)
            root = _find_skill_root(tmp)
            if root is None:
                raise ValueError(
                    "压缩包内未找到 SKILL.md（需在根目录或唯一顶层目录内）")
            text = (root / "SKILL.md").read_text(
                encoding="utf-8", errors="replace")
            meta = _parse_front_matter(text)
            candidates = [
                str(meta.get("name", "")),
                "" if root == tmp else root.name,
                Path(str(fallback_name or "")).stem,
            ]
            name = ""
            for candidate in candidates:
                name = sanitize_skill_name(candidate)
                if name:
                    break
            if not name:
                raise ValueError(
                    "无法确定 skill 名称（SKILL.md 的 name 缺失或非法，"
                    "且顶层目录名/上传文件名也不可用）")
            target = Path(skills_dir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = ""
            if target.exists():
                backup = _move_away(target, Path(backup_dir), name)
                logger.info("skill old version backed up: %s", backup)
            try:
                shutil.move(str(root), str(target))
            except Exception:
                if backup:  # 尽力恢复旧版，避免两空
                    try:
                        shutil.move(backup, str(target))
                    except Exception:  # noqa: BLE001
                        logger.error("skill restore failed; backup kept at %s",
                                     backup)
                raise
            logger.info("skill installed name=%s files=%d bytes=%d",
                        name, files, total)
            return {"name": name, "files": files, "bytes": total,
                    "backup": backup}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def delete_skill(name: str, *, skills_dir: str, trash_dir: str) -> dict[str, Any]:
    """删除 skill：移入回收目录（可手动恢复），失败抛 ValueError。"""
    safe = sanitize_skill_name(name)
    if not safe:
        raise ValueError(f"非法 skill 名称: {name!r}")
    target = Path(skills_dir) / safe
    if not target.is_dir():
        raise ValueError(f"skill 不存在: {safe}")
    dest = _move_away(target, Path(trash_dir), safe)
    logger.info("skill moved to trash: %s", dest)
    return {"name": safe, "trashed_to": dest}
