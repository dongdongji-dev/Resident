"""System prompt assembly（subagent 版，适配 resident 拼接结构）。

三层拼接，顺序固定：
1. 一句话身份头：项目名 + 真实模型 + 用户工作目录（来自 config，不硬编码）；
2. config.prompt.files 索引的提示词文件（按声明顺序，缺失跳过并告警）；
3. 已注册工具的自动生成定义（name + 参数 + description）。

与 resident 原版唯一差异：去掉 skills 层（subagent 暂无 SKILL.md 扫描）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.prompts")


def build_identity_header(project_name: str, model: str,
                          user_dir: Optional[str] = None) -> str:
    """一句话项目身份头。值来自 config（不硬编码）。"""
    line = f"你在 {project_name} 项目里，你的真实模型为：{model}。"
    if user_dir:
        line += f"\n你的用户工作目录为（容器内实际路径）：{user_dir}"
    return line


def load_prompt_files(prompt_dir: str, files: list[str]) -> str:
    """按声明顺序加载 config-indexed 提示词文件；缺失跳过并告警。"""
    parts: list[str] = []
    for name in files:
        path = os.path.join(prompt_dir, str(name))
        if not os.path.exists(path):
            logger.warning("system prompt file missing: %s", path)
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read().strip()
            if content:
                parts.append(content)
        except OSError as exc:
            logger.warning("failed to read system prompt %s: %s", path, exc)
    return "\n\n".join(parts)


def build_tools_section(tool_schemas: list[dict[str, Any]]) -> str:
    """工具定义从已注册工具自动生成。"""
    if not tool_schemas:
        return ""
    lines = ["# 可用工具", ""]
    for schema in tool_schemas:
        function = schema.get("function") or {}
        name = function.get("name", "")
        description = (function.get("description") or "").strip()
        parameters = function.get("parameters") or {}
        props = (parameters.get("properties") or {}) if isinstance(
            parameters, dict) else {}
        if props:
            param_repr = "".join(
                f"\n  - {pname}({spec.get('type', 'any')})"
                for pname, spec in props.items())
        else:
            param_repr = "（无参数）"
        lines.append(f"### {name}{param_repr}")
        if description:
            lines.append(description)
        lines.append("")
    return "\n".join(lines).strip()


def build_skills_section(skill_infos: list[dict]) -> str:
    """技能清单（区分全局/局部），格式对齐 resident 系统提示词技能目录。"""
    if not skill_infos:
        return ""
    lines = ["## 可用技能", ""]
    scope_labels = {"global": "（全局）", "local": "（局部）"}
    for info in skill_infos:
        name = info.get("name", "")
        scope = info.get("scope", "")
        label = scope_labels.get(scope, "")
        meta = info.get("meta") or {}
        desc = (meta.get("description") or "").strip()
        trigger = (meta.get("trigger") or "").strip()
        lines.append(f"skill 目录：{name}{label}")
        lines.append("---")
        lines.append(f"name: {name}")
        if desc:
            lines.append(f'description: "{desc}"')
        if trigger:
            lines.append(f'trigger: "{trigger}"')
        lines.append("---")
        lines.append("")
    return "\n".join(lines).strip()


def build_system_prompt(project_name: str, model: str,
                        user_dir: Optional[str],
                        prompt_dir: str, files: list[str],
                        tool_schemas: list[dict[str, Any]],
                        skills: Optional[list[dict]] = None) -> str:
    """拼接完整系统提示词：身份头 → 配置提示词文件 → 技能清单 → 工具定义。"""
    parts: list[str] = []
    header = build_identity_header(project_name, model, user_dir)
    if header:
        parts.append(header)
    loaded = load_prompt_files(prompt_dir, files)
    if loaded:
        parts.append(loaded)
    skill_section = build_skills_section(skills or [])
    if skill_section:
        parts.append(skill_section)
    tools = build_tools_section(tool_schemas)
    if tools:
        parts.append(tools)
    return "\n\n".join(parts)