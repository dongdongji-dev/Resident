"""System prompt assembly.

The Agent's system prompt is built from four layers, in order:

1. a one-line project summary (project name + real model + user/work dir
   inside the container) — auto-generated from config;
2. the prompt files the config loads (```prompt.files```), in the declared
   order, from shared system definitions and user-custom definitions;
3. the runtime message-markers block (``runtime_block``, optional) —
   generated from the CURRENT config by ``runtime_block.build_runtime_block``
   so the described markers always match what the system actually injects;
4. the tool definitions (name + params + description) auto-generated from
   the registered tools in the registry.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.prompts")


def build_identity_header(project_name: str, model: str,
                          user_dir: Optional[str] = None) -> str:
    """One-line project summary. Values come from config (no hardcoding)."""
    line = f"你在 {project_name} 项目里，你的真实模型为：{model}。"
    if user_dir:
        line += f"\n你的用户工作目录为（容器内实际路径）：{user_dir}"
    return line


def load_prompt_files(prompt_dir: str,
                      files: list[str]) -> str:
    """Load the config-indexed prompt files in order; missing files are
    skipped with a warning (same contract as the old project)."""
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
    """Tool definitions auto-generated from the registered tools."""
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


def build_system_prompt(project_name: str, model: str,
                        user_dir: Optional[str],
                        prompt_dir: str, files: list[str],
                        tool_schemas: list[dict[str, Any]],
                        skills_dir: Optional[str] = None,
                        runtime_block: Optional[str] = None) -> str:
    """Assemble the full system prompt: header -> config files -> runtime
    block (when provided) -> skills (when configured) -> tools."""
    parts: list[str] = []
    header = build_identity_header(project_name, model, user_dir)
    if header:
        parts.append(header)
    loaded = load_prompt_files(prompt_dir, files)
    if loaded:
        parts.append(loaded)
    if runtime_block:
        parts.append(runtime_block)
    if skills_dir:
        from kernel.agent_framework.skills import skills_prompt
        skills = skills_prompt(skills_dir)
        if skills:
            parts.append(skills)
    tools = build_tools_section(tool_schemas)
    if tools:
        parts.append(tools)
    return "\n\n".join(parts)


def make_system_prompt_builder(
        project_name: str, model: str,
        user_dir: Optional[str],
        prompt_dir: str, files: list[str],
        tool_schemas_provider: Callable[[], list[dict[str, Any]]],
        skills_dir: Optional[str] = None,
        runtime_block: Optional[str] = None) -> Callable[[], str]:
    """Return a callable that re-renders the full system prompt from the
    CURRENT registry state on every call.

    The tool registry is the single source of truth: the static
    "# 可用工具" section can never go stale no matter when tools are
    registered (the old build_agent() assembled the prompt BEFORE later
    tool registrations, so its tool list was incomplete)."""

    def build() -> str:
        return build_system_prompt(
            project_name=project_name, model=model, user_dir=user_dir,
            prompt_dir=prompt_dir, files=files,
            tool_schemas=list(tool_schemas_provider()),
            skills_dir=skills_dir, runtime_block=runtime_block)

    return build
