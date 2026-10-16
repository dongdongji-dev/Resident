"""agent_framework — platform-neutral agent framework（subagent 从0重建版）.

- tool registration + ReAct loop（4 态状态机：IDLE/STREAMING/TOOL_USE/COMPRESS）
- multi-protocol LLM adapters（llm/：openai-compatible / anthropic / responses）
- thread-safe event queue + explicit state machine
- JSONL context store with tail repair
- tool result 状态码层（非200 写 context 不触发 call）
"""

from __future__ import annotations

from kernel.agent_framework.agent import Agent, AgentState
from kernel.agent_framework.events import Event, EventQueue

__all__ = ["Agent", "AgentState", "Event", "EventQueue"]