"""OpenAI-compatible Chat Completions backend with per-variant adaptation.

OpenAI-compatible providers share ``/chat/completions`` but differ subtly:
- the reasoning/thinking chain comes back under different delta fields
  (``reasoning_content`` / ``reasoning`` / …) and sometimes as a list of
  object fragments instead of a plain string;
- the reasoning field accepted on the *request* side for continued context
  also varies.

``llm.variant`` picks a default set of those field names; ``llm.options`` /
``llm.extra_params`` from config override/extend the request payload.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Optional

import aiohttp
import certifi

from kernel.agent_framework.content import gate_modalities
from kernel.agent_framework.llm.base import (DeltaCallback, LLMPermanentError,
                                             LLMResult,
                                             RequestTooLargeError, ToolCall,
                                             is_request_too_large,
                                             merge_options,
                                             stream_with_retry)
from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.llm.openai_compatible")


# name -> adapter knobs. Vendors can add entries; config can override.
_VARIANTS: dict[str, dict[str, Any]] = {
    "generic": {
        "thinking_request_key": "reasoning_content",
        "thinking_response_fields": ("reasoning_content", "reasoning"),
    },
    "openai": {
        "thinking_request_key": "reasoning_content",
        "thinking_response_fields": ("reasoning_content", "reasoning"),
    },
    "deepseek": {
        "thinking_request_key": "reasoning_content",
        "thinking_response_fields": ("reasoning_content", "reasoning"),
    },
    "qwen": {
        "thinking_request_key": "reasoning_content",
        "thinking_response_fields": ("reasoning_content", "reasoning"),
    },
    "glm": {
        "thinking_request_key": "reasoning_content",
        "thinking_response_fields": ("reasoning_content",),
    },
}

# Fields allowed on messages sent to a Chat Completions backend.
_ALLOWED_MESSAGE_KEYS = ("role", "content", "reasoning_content",
                         "tool_calls", "tool_call_id", "name")


def _chat_message(message: dict[str, Any],
                  thinking_request_key: str) -> dict[str, Any]:
    """Drop protocol-specific state; map the reasoning field to the key the
    variant expects on the request."""
    out: dict[str, Any] = {}
    for key in _ALLOWED_MESSAGE_KEYS:
        if key in message and key not in ("reasoning_content",):
            out[key] = message[key]
    thinking = message.get("reasoning_content")
    if thinking:
        out[thinking_request_key] = thinking
    return out


def _extract_thinking(delta: dict[str, Any],
                      fields: tuple[str, ...]) -> str:
    """Read the reasoning delta under any of the variant's candidate fields;
    supports both plain strings and a list of ``{"text": ...}`` fragments."""
    for name in fields:
        value = delta.get(name)
        if value is None:
            continue
        if isinstance(value, list):
            return "".join(str(item.get("text", ""))
                           for item in value if isinstance(item, dict))
        return str(value)
    return ""


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_seconds: float, retries: int,
                 retry_delay_seconds: float,
                 variant: str = "generic",
                 thinking_request_key: Optional[str] = None,
                 thinking_response_fields: Optional[tuple[str, ...]] = None,
                 extra: Optional[dict[str, Any]] = None,
                 proxy: Optional[str] = None,
                 backoff_base: float = 2.0,
                 cap_seconds: float = 60.0,
                 user_agent: str = "",
                 session_id: str = "",
                 input_modalities: Optional[list[str]] = None) -> None:
        knobs = _VARIANTS.get(variant, _VARIANTS["generic"])
        self.user_agent = str(user_agent or "")
        self.session_id = str(session_id or "")
        self.input_modalities = tuple(input_modalities) \
            if input_modalities is not None else ("text",)
        self.thinking_request_key = (thinking_request_key
                                     or knobs["thinking_request_key"])
        self.thinking_response_fields = tuple(
            thinking_response_fields or knobs["thinking_response_fields"])
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(0, int(retries))
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.extra = dict(extra or {})
        self.proxy = proxy
        self.backoff_base = float(backoff_base)
        self.cap_seconds = float(cap_seconds)
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    _RETRYABLE = (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError,
                  aiohttp.ServerDisconnectedError, aiohttp.ClientResponseError,
                  asyncio.TimeoutError, ConnectionResetError)

    def build_payload(self, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]]) -> dict[str, Any]:
        gated: list[dict[str, Any]] = []
        for message in messages:
            item = dict(message)
            item["content"] = gate_modalities(
                message.get("content", ""), self.input_modalities)
            gated.append(item)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_chat_message(m, self.thinking_request_key)
                         for m in gated],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        payload.update(self.extra)
        return payload

    async def stream(self, messages: list[dict[str, Any]],
                     tools: Optional[list[dict[str, Any]]] = None,
                     on_delta: Optional[DeltaCallback] = None) -> LLMResult:
        payload = self.build_payload(messages, tools or [])
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.session_id:
            headers["x-opencode-session"] = self.session_id

        async def attempt():
            return await self._stream_once(payload, headers, on_delta)

        return await stream_with_retry(
            attempt,
            retries=self.retries,
            retry_delay_seconds=self.retry_delay_seconds,
            retryable=self._RETRYABLE,
            backoff_base=self.backoff_base,
            cap_seconds=self.cap_seconds,
            description="openai-compatible LLM",
            on_retry=lambda n, e: logger.warning(
                "openai-compatible retry %d/%d: %s", n, self.retries + 1, e),
        )

    async def _stream_once(self, payload: dict[str, Any],
                           headers: dict[str, str],
                           on_delta: Optional[DeltaCallback]) -> LLMResult:
        result = LLMResult()
        calls: dict[int, dict[str, str]] = {}
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/chat/completions", json=payload,
                headers=headers, proxy=self.proxy, ssl=self.ssl_context,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    if is_request_too_large(response.status, body):
                        raise RequestTooLargeError(
                            f"openai-compatible HTTP {response.status}: "
                            f"{body[:300]}")
                    if response.status in (408, 429) or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message=body[:300])
                    raise LLMPermanentError(
                        f"LLM HTTP {response.status}: {body[:300]}")
                async for raw in response.content:
                    for line in raw.decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("usage"):
                            result.usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            result.text += str(content)
                            if on_delta:
                                on_delta("content", str(content))
                        thinking = _extract_thinking(
                            delta, self.thinking_response_fields)
                        if thinking:
                            result.thinking += thinking
                            if on_delta:
                                on_delta("thinking", thinking)
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index", 0))
                            slot = calls.setdefault(
                                index, {"id": "", "name": "", "arguments": ""})
                            if call.get("id"):
                                slot["id"] = str(call["id"])
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["name"] = str(function["name"])
                            if function.get("arguments"):
                                slot["arguments"] += str(function["arguments"])
        result.tool_calls = [
            ToolCall(call_id=slot["id"], name=slot["name"],
                     arguments=slot["arguments"] or "{}")
            for _, slot in sorted(calls.items()) if slot["name"]
        ]
        return result
