"""agent_framework — platform-neutral agent framework.

- tool registration + ReAct loop
- multi-protocol LLM adapters (openai-compatible variants / anthropic / openai)
- thread-safe event queue + explicit state machine
- JSONL context store with tail repair
- strict YAML config (every parameter lives in the config file)
"""

from __future__ import annotations

from kernel.agent_framework.agent import Agent, AgentState
from kernel.agent_framework.events import Event, EventQueue

__all__ = ["Agent", "AgentState", "Event", "EventQueue"]
