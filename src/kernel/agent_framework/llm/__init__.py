"""LLM adapters — provider-neutral protocol + factory.

Every adapter implements the same async ``stream()`` contract. The factory
selects the wire protocol from config:
- ``openai-compatible`` + ``variant`` (generic/deepseek/qwen/...) → Chat
  Completions with per-variant reasoning-field handling
- ``anthropic`` → Messages API (tool_use / tool_result)
- ``openai`` → Responses API (instruction + input items)
"""

from __future__ import annotations

from kernel.agent_framework.llm.base import (
    DeltaCallback, LLMClient, LLMResult, ToolCall, merge_options)
from kernel.agent_framework.llm.factory import create_llm_client

__all__ = ["LLMClient", "LLMResult", "ToolCall", "DeltaCallback",
           "merge_options", "create_llm_client"]
