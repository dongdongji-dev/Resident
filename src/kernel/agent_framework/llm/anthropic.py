"""Anthropic Messages API backend (/messages, x-api-key auth).

Converts the neutral message list (system/user/assistant/tool) into the
Anthropic ``system`` + message + ``tool_use``/``tool_result`` structure and
streams ``content_block_*`` events into an ``LLMResult``.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Optional

import aiohttp
import certifi

from kernel.agent_framework.content import (gate_modalities, image_url,
                                            is_image_block, placeholder, text_of)
from kernel.agent_framework.llm.base import (DeltaCallback, LLMPermanentError,
                                             LLMResult,
                                             RequestTooLargeError, ToolCall,
                                             is_request_too_large,
                                             merge_options, stream_with_retry)
from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.llm.anthropic")


def _data_url_parts(url: str) -> tuple[str, str, bool]:
    """``data:<media>;base64,<payload>`` → (media, payload, is_base64)."""
    header, _, data = str(url or "").partition(",")
    meta = header[len("data:"):] if header.startswith("data:") else ""
    parts = meta.split(";")
    media_type = parts[0] or "application/octet-stream"
    is_base64 = any(part.strip().lower() == "base64" for part in parts[1:])
    return media_type, data, is_base64


def _anthropic_content(content: Any) -> list[dict[str, Any]]:
    """Neutral content (str or blocks) → Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, dict):
        content = [content]
    blocks: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif is_image_block(block):
            url = image_url(block)
            if url.startswith("data:"):
                media_type, data, is_base64 = _data_url_parts(url)
                if data and is_base64:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64",
                                   "media_type": media_type, "data": data},
                    })
                else:
                    blocks.append({"type": "text",
                                   "text": placeholder(url)})
            elif url:
                blocks.append({"type": "image",
                               "source": {"type": "url", "url": url}})
    return blocks


def _anthropic_messages(messages: list[dict[str, Any]]):
    system: list[str] = []
    result: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", ""))
        if role in ("system", "developer"):
            if message.get("content"):
                system.append(text_of(message["content"]))
            continue
        if role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id", "")),
                    "content": text_of(message.get("content", "")),
                }],
            })
            continue
        content = _anthropic_content(message.get("content"))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    arguments = {}
                content.append({
                    "type": "tool_use",
                    "id": str(call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "input": arguments,
                })
        if len(content) == 1 and content[0].get("type") == "text":
            result.append({"role": role, "content": content[0]["text"]})
        else:
            result.append({"role": role, "content": content or ""})
    return "\n\n".join(system), result


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for tool in tools:
        function = tool.get("function") or tool
        out.append({
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "input_schema": function.get("parameters") or {
                "type": "object", "properties": {}},
        })
    return out


class AnthropicLLM:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_seconds: float, retries: int,
                 retry_delay_seconds: float,
                 extra: Optional[dict[str, Any]] = None,
                 proxy: Optional[str] = None,
                 backoff_base: float = 2.0,
                 cap_seconds: float = 60.0,
                 user_agent: str = "",
                 session_id: str = "",
                 input_modalities: Optional[list[str]] = None,
                 stream_idle_timeout_seconds: float = 90.0) -> None:
        self.user_agent = str(user_agent or "")
        self.session_id = str(session_id or "")
        self.input_modalities = tuple(input_modalities) \
            if input_modalities is not None else ("text",)
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(0, int(retries))
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.extra = dict(extra or {})
        self.proxy = proxy
        self.backoff_base = float(backoff_base)
        self.cap_seconds = float(cap_seconds)
        self.stream_idle_timeout_seconds = float(
            stream_idle_timeout_seconds or 0)
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
        system, converted = _anthropic_messages(gated)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": 8192,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _anthropic_tools(tools)
        payload.update(self.extra)
        return payload

    async def stream(self, messages: list[dict[str, Any]],
                     tools: Optional[list[dict[str, Any]]] = None,
                     on_delta: Optional[DeltaCallback] = None) -> LLMResult:
        payload = self.build_payload(messages, tools or [])
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
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
            description="Anthropic Messages",
            on_retry=lambda n, e: logger.warning(
                "anthropic retry %d/%d: %s", n, self.retries + 1, e),
        )

    async def _stream_once(self, payload: dict[str, Any],
                           headers: dict[str, str],
                           on_delta: Optional[DeltaCallback]) -> LLMResult:
        result = LLMResult()
        calls: dict[int, dict[str, str]] = {}
        current_block = -1
        idle = self.stream_idle_timeout_seconds
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            sock_read=idle if idle > 0 else None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/messages", json=payload,
                headers=headers, proxy=self.proxy, ssl=self.ssl_context,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    if is_request_too_large(response.status, body):
                        raise RequestTooLargeError(
                            f"Anthropic HTTP {response.status}: {body[:300]}")
                    if response.status in (408, 429) or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message=body[:300])
                    raise LLMPermanentError(
                        f"Anthropic HTTP {response.status}: {body[:300]}")
                async for raw in response.content:
                    for line in raw.decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        event_type = event.get("type", "")
                        if event_type == "message_start":
                            usage = (event.get("message") or {}).get("usage")
                            if usage:
                                result.usage = dict(usage)
                        elif event_type == "content_block_start":
                            current_block = int(event.get("index", -1))
                            block = event.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                calls[current_block] = {
                                    "id": str(block.get("id", "")),
                                    "name": str(block.get("name", "")),
                                    "arguments": "",
                                }
                        elif event_type == "content_block_delta":
                            delta = event.get("delta") or {}
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                text = str(delta.get("text", ""))
                                result.text += text
                                if on_delta and text:
                                    on_delta("content", text)
                            elif dtype == "input_json_delta":
                                if current_block in calls:
                                    calls[current_block]["arguments"] += str(
                                        delta.get("partial_json", ""))
                            elif dtype in ("thinking_delta", "signature_delta"):
                                thinking = str(delta.get("thinking", ""))
                                result.thinking += thinking
                                if on_delta and thinking:
                                    on_delta("thinking", thinking)
        result.tool_calls = [
            ToolCall(call_id=slot["id"], name=slot["name"],
                     arguments=slot["arguments"] or "{}")
            for _, slot in sorted(calls.items()) if slot["name"]
        ]
        return result
