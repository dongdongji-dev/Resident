"""context_compress — model-triggered compression request.

The Agent sets ``system_vars.compress_requested = "true"`` and the ReAct loop
performs the actual compression at the next SAFE point (round start / end
with an empty event queue). Compression is never run inside the tool itself:
a tool timeout would cancel it mid-flight after the archive step and could
lose history, and running it mid-round would orphan queued tool results.
"""

from __future__ import annotations

from typing import Any

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.compress_tools")


def make_context_compress(system_vars: Any) -> Any:
    def context_compress() -> str:
        """请求压缩当前上下文（释放 token 与字节预算）。

        用途: 当上下文过长、或 readimage 因字节预算不足失败时调用。调用后
        不会立即压缩——Agent 会在本轮结束后/下一轮开始前的安全点执行，
        请等待压缩完成后再继续（例如再次尝试 readimage）。

        返回: 固定为触发成功提示。
        """
        if system_vars is None:
            return "触发失败：系统变量存储不可用"
        try:
            system_vars.set("compress_requested", "true")
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_compress flag set failed: %s", exc)
            return f"触发失败：{exc}"
        logger.info("context_compress tool requested compression")
        return "已触发压缩：请等待压缩完成（将在本轮结束后的安全点执行）"

    return context_compress
