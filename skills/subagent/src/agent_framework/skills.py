"""技能（skill）扫描与解析（subagent 版，设计约定）。

两类技能来源：
- 全局：``subagent/config/skill/<name>/SKILL.md``（所有实例共用）。
- 局部：工作区 ``<workdir>/.skill/<name>/SKILL.md``（工作区专属）。

SKILL.md 格式对齐 resident（我的系统提示词技能目录）：三行 frontmatter
``name`` / ``description``（可选 trigger） + 正文。每个技能一个子目录。

load_skill 解析优先级（原设计说明）：
- 某技能名全局有局部没有 → 加载全局；
- 只有局部没有全局 → 加载局部；
- 局部全局都有 → 加载调用参数 scope 指定的那个（默认局部）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("subagent.skills")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    trigger: str = ""


@dataclass(frozen=True)
class SkillInfo:
    name: str
    scope: str            # "global" | "local"
    meta: Optional[SkillMeta]
    full_path: str


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_skill_md(text: str) -> Optional[SkillMeta]:
    """解析 SKILL.md frontmatter，返回 name/description/trigger。"""
    if not text:
        return None
    m = _FM_RE.match(text)
    if not m:
        return None
    body = m.group(1)
    name = ""
    description = ""
    trigger = ""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"\'')
        elif line.startswith("description:"):
            description = line[len("description:"):].strip().strip(
                '"\'')
        elif line.startswith("trigger:"):
            trigger = line[len("trigger:"):].strip().strip('"\'')
    if not name:
        return None
    return SkillMeta(name=name, description=description, trigger=trigger)


def skill_files_in(root: Path) -> list[Path]:
    """扫描 root 下的所有 <name>/SKILL.md。root 不存在返回空列表。"""
    if root is None or not root.is_dir():
        return []
    return sorted(
        p / "SKILL.md" for p in root.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file())


def _skill_list(root: Optional[Path], scope: str) -> list[SkillInfo]:
    out: list[SkillInfo] = []
    if root is None:
        return out
    for md in skill_files_in(Path(root)):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("read skill failed %s: %s", md, exc)
            continue
        out.append(SkillInfo(
            name=md.parent.name,
            scope=scope,
            meta=parse_skill_md(text),
            full_path=str(md),
        ))
    return out


def scan_skills(local_root: Optional[Path],
                global_root: Optional[Path]) -> list[SkillInfo]:
    """扫描局部+全局技能列表。局部在前（优先展示）。"""
    return _skill_list(local_root, "local") + _skill_list(global_root, "global")


def find_skill(name: str, scope: str,
               local_root: Optional[Path],
               global_root: Optional[Path]) -> Optional[SkillInfo]:
    """按 load_skill 解析优先级找技能。

    优先级（设计约定）：
    - 全局有局部没有 → 全局；
    - 只有局部没有全局 → 局部；
    - 局部全局都有 → scope 参数指定的（默认 local）。
    """
    local = _find_in(local_root, name)
    global_ = _find_in(global_root, name)
    if local and global_:
        want = scope if scope in ("local", "global") else "local"
        return local if want == "local" else global_
    return local or global_


def _find_in(root: Optional[Path], name: str) -> Optional[SkillInfo]:
    if root is None:
        return None
    for info in _skill_list(Path(root), "local" if "workspace" in str(root)
                            else "global"):
        if info.name == name:
            return info
    return None


def load_skill_content(name: str, scope: str,
                       local_root: Optional[Path],
                       global_root: Optional[Path]) -> Optional[str]:
    """加载技能全文（SKILL.md 原始内容）；未找到返回 None。"""
    info = find_skill(name, scope, local_root, global_root)
    if info is None:
        return None
    try:
        return Path(info.full_path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("read skill %s failed: %s", name, exc)
        return None