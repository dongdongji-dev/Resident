"""LLM client factory — protocol selected explicitly from config."""

from __future__ import annotations

from typing import Any

from kernel.agent_framework.llm.base import LLMClient, merge_options


def create_llm_client(config: dict[str, Any]) -> LLMClient:
    protocol = str(config.get("protocol", "openai-compatible")).lower()
    kwargs = {
        "base_url": config["base_url"],
        "api_key": config["api_key"],
        "model": config["model"],
        "timeout_seconds": config.get("timeout_seconds", 600),
        "retries": config.get("retries", 2),
        "retry_delay_seconds": config.get("retry_delay_seconds", 1.5),
        "extra": merge_options(config.get("options"),
                               config.get("extra_params")),
        "proxy": config.get("proxy"),
        "backoff_base": float(config.get("backoff_base", 2.0)),
        "cap_seconds": float(config.get("retry_cap_seconds",
                               config.get("cap_seconds", 60.0))),
    }
    if protocol in ("openai", "responses", "openai-responses"):
        from kernel.agent_framework.llm.responses import OpenAILLM
        return OpenAILLM(**kwargs)
    if protocol in ("anthropic", "messages"):
        from kernel.agent_framework.llm.anthropic import AnthropicLLM
        return AnthropicLLM(**kwargs)
    if protocol in ("openai-compatible", "chat-completions", "compatible"):
        from kernel.agent_framework.llm.openai_compatible import (
            OpenAICompatibleLLM)
        kwargs["variant"] = config.get("variant", "generic")
        kwargs["extra"] = merge_options(config.get("options"),
                                        config.get("extra_params"))
        return OpenAICompatibleLLM(**kwargs)
    if protocol in ("google", "gemini", "generate-content"):
        from kernel.agent_framework.llm.google import GoogleLLM
        return GoogleLLM(**kwargs)
    raise ValueError(
        f"unsupported LLM protocol {protocol!r}; expected openai, "
        "openai-compatible, anthropic, or google")
