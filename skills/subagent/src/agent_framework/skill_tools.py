"""load_skill 工具（设计约定）。

每次 agent 启动时系统提示词已自动扫描全局 + 局部 skill 的 description；
load_skill 用于在运行时把某个 skill 的完整 SKILL.md 正文加载进上下文
（类似我的 system prompt 里的 skills_load）。

优先级（原设计说明）：
- 全局有局部没有 → 加载全局；
- 只有局部没有全局 → 加载局部；
- 局部全局都有 → 加载 scope 参数指定的那个（默认局部）。

局部 skill 根 = 当前 agent 工作目录的 ``.skill/``；因 ToolRegistry 是 hub 级
共享、局部根因 agent 而异，用线程局部变量（threading.local）在 agent 的
_loop_runner 里设置（每个 agent 独立线程，互不干扰）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from kernel.agent_framework import skills as _skills

logger = __import__("logging").getLogger("subagent.skill_tools")

_thread = threading.local()


def set_session_skill_root(local_root: Optional[Path]) -> None:
    """在 agent 线程内设置局部 skill 根（workdir/.skill）。"""
    _thread.local_root = local_root


def _current_local_root() -> Optional[Path]:
    return getattr(_thread, "local_root", None)


def load_skill(name: str, scope: str = "local",
               global_root: Optional[Path] = None) -> str:
    """加载指定技能原文（SKILL.md）。参数:
    name: 技能名；scope: local(默认)/global——局部全局都有时优先用此参数指定的；
    全局有局部没有→加载全局；只有局部→加载局部。返回技能全文或未找到提示。"""
    local_root = _current_local_root()
    content = _skills.load_skill_content(
        name, scope, local_root, global_root)
    if content is None:
        return (f"未找到技能「{name}」（局部 {bool(local_root)} / "
                f"全局 {bool(global_root)}）。可用技能见系统提示词技能清单。")
    return content


def register_skill_tools(registry, global_root: Optional[Path]) -> None:
    """注册 load_skill 工具。global_root 在 hub 启动时确定（config/skill）。
    用闭包而非 partial 绑定 global_root，避免参数泄漏进工具 schema。
    用 async handler：ToolRegistry.invoke 对 coroutine 直接 await（不
    to_thread 换线程），保证局部 skill 根的 threading.local 在 agent loop
    线程内可见。"""

    async def _load(name: str, scope: str = "local") -> str:
        """加载指定技能原文（SKILL.md）。参数:
        name: 技能名；scope: local(默认)/global——局部全局都有时优先用此参数
        指定的；全局有局部没有→加载全局；只有局部→加载局部。"""
        return load_skill(name, scope, global_root=global_root)

    registry.register("load_skill", _load)