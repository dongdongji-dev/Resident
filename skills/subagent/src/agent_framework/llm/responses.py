"""OpenAI Responses API backend (/responses).

Reasoning and function calls are output *items* rather than Chat-Completion
delta fields. This adapter normalizes visible text + tool calls into an
``LLMResult`` while persisting raw output items so encrypted reasoning can
be continued later.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Optional

import aiohttp
import certifi

from kernel.agent_framework.llm.base import (DeltaCallback, LLMResult,
                                             ToolCall, stream_with_retry)
from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.llm.responses")


def _response_input(messages: list[dict[str, Any]]):
    instructions: list[str] = []
    inputs: list[Any] = []
    for message in messages:
        role = str(message.get("role", ""))
        if role in ("system", "developer"):
            if message.get("content"):
                instructions.append(str(message["content"]))
            continue
        provider_items = message.get("provider_items") or []
        if provider_items:
            inputs.extend(provider_items)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            if call_id:
                inputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(message.get("content", "")),
                })
            continue
        if role == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                inputs.append({
                    "type": "function_call",
                    "call_id": str(call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "arguments": str(function.get("arguments", "{}")),
                })
            if message.get("content"):
                inputs.append({"role": "assistant",
                               "content": str(message["content"])})
            continue
        if role in ("user", "assistant"):
            inputs.append({"role": role,
                           "content": message.get("content", "")})
    return "\n\n".join(instructions), inputs


def _response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for tool in tools:
        function = tool.get("function") or tool
        item = {
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters") or {
                "type": "object", "properties": {}},
        }
        if "strict" in function:
            item["strict"] = function["strict"]
        out.append(item)
    return out


def _reasoning_summary_text(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") != "reasoning":
            continue
        for summary in item.get("summary") or []:
            if isinstance(summary, dict) and summary.get("text"):
                parts.append(str(summary["text"]))
    return "\n\n".join(parts)


class OpenAILLM:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_seconds: float, retries: int,
                 retry_delay_seconds: float,
                 extra: Optional[dict[str, Any]] = None,
                 proxy: Optional[str] = None,
                 backoff_base: float = 2.0,
                 cap_seconds: float = 60.0) -> None:
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
        instructions, inputs = _response_input(messages)
        payload: dict[str, Any] = {"model": self.model, "input": inputs,
                                   "stream": True}
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = _response_tools(tools)
        payload.update(self.extra)
        return payload

    async def stream(self, messages: list[dict[str, Any]],
                     tools: Optional[list[dict[str, Any]]] = None,
                     on_delta: Optional[DeltaCallback] = None) -> LLMResult:
        payload = self.build_payload(messages, tools or [])
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}

        async def attempt():
            return await self._stream_once(payload, headers, on_delta)

        return await stream_with_retry(
            attempt,
            retries=self.retries,
            retry_delay_seconds=self.retry_delay_seconds,
            retryable=self._RETRYABLE,
            backoff_base=self.backoff_base,
            cap_seconds=self.cap_seconds,
            description="OpenAI Responses",
            on_retry=lambda n, e: logger.warning(
                "openai responses retry %d/%d: %s", n, self.retries + 1, e),
        )

    async def _stream_once(self, payload: dict[str, Any],
                           headers: dict[str, str],
                           on_delta: Optional[DeltaCallback]) -> LLMResult:
        result = LLMResult()
        calls: dict[str, dict[str, str]] = {}
        items: dict[str, dict[str, Any]] = {}
        completed_output: list[dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/responses", json=payload,
                headers=headers, proxy=self.proxy, ssl=self.ssl_context,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    if response.status in (408, 429) or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message=body[:300])
                    raise RuntimeError(
                        f"OpenAI Responses HTTP {response.status}: {body[:300]}")
                async for raw in response.content:
                    for line in raw.decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        try:
                            event = json.loads(data)
                        except ValueError:
                            continue
                        self._handle_event(
                            event, result, calls, items, on_delta)
                        if event.get("type") == "response.completed":
                            response_data = event.get("response") or {}
                            if response_data.get("usage"):
                                result.usage = response_data["usage"]
                            completed_output = list(
                                response_data.get("output") or [])
                            if not result.thinking:
                                result.thinking = _reasoning_summary_text(
                                    completed_output)
        result.provider_items = completed_output or list(items.values())
        result.tool_calls = [
            ToolCall(call_id=value["id"], name=value["name"],
                     arguments=value["arguments"] or "{}")
            for value in calls.values() if value.get("name")
        ]
        return result

    @staticmethod
    def _handle_event(event: dict[str, Any], result: LLMResult,
                      calls: dict[str, dict[str, str]],
                      items: dict[str, dict[str, Any]],
                      on_delta: Optional[DeltaCallback]) -> None:
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            text = str(event.get("delta", ""))
            result.text += text
            if on_delta and text:
                on_delta("content", text)
        elif event_type == "response.output_text.done":
            if not result.text:
                result.text = str(event.get("text", ""))
        elif event_type in ("response.reasoning_text.delta",
                            "response.reasoning_summary_text.delta"):
            thinking = str(event.get("delta", ""))
            result.thinking += thinking
            if on_delta and thinking:
                on_delta("thinking", thinking)
        elif event_type in ("response.reasoning_text.done",
                            "response.reasoning_summary_text.done"):
            if not result.thinking:
                result.thinking = str(event.get("text", ""))
        elif event_type in ("response.output_item.added",
                            "response.output_item.done"):
            item = event.get("item") or {}
            item_id = str(item.get("id", event.get("item_id", "")))
            if item_id:
                items[item_id] = item
                if item.get("type") == "function_call":
                    slot = calls.setdefault(item_id, {
                        "id": str(item.get("call_id", item_id)),
                        "name": "", "arguments": ""})
                    slot["name"] = str(item.get("name", slot["name"]))
                    if item.get("arguments"):
                        slot["arguments"] = str(item["arguments"])
        elif event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id", ""))
            slot = calls.setdefault(item_id, {
                "id": item_id, "name": "", "arguments": ""})
            slot["arguments"] += str(event.get("delta", ""))
        elif event_type == "response.function_call_arguments.done":
            item_id = str(event.get("item_id", ""))
            slot = calls.setdefault(item_id, {
                "id": item_id, "name": "", "arguments": ""})
            slot["name"] = str(event.get("name", slot["name"]))
            slot["arguments"] = str(event.get("arguments", slot["arguments"]))
