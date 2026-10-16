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

from kernel.agent_framework.llm.base import (DeltaCallback, LLMResult,
                                             ToolCall, merge_options,
                                             stream_with_retry)
from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.llm.anthropic")


def _anthropic_messages(messages: list[dict[str, Any]]):
    system: list[str] = []
    result: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", ""))
        if role in ("system", "developer"):
            if message.get("content"):
                system.append(str(message["content"]))
            continue
        if role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id", "")),
                    "content": str(message.get("content", "")),
                }],
            })
            continue
        content: list[dict[str, Any]] = []
        if message.get("content"):
            content.append({"type": "text", "text": str(message["content"])})
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
        system, converted = _anthropic_messages(messages)
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
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/messages", json=payload,
                headers=headers, proxy=self.proxy, ssl=self.ssl_context,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    if response.status in (408, 429) or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message=body[:300])
                    raise RuntimeError(
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
                        elif event_type == "message_delta":
                            # 最终 output_tokens 只在 message_delta 里给（message_start
                            # 里的 output_tokens 是起手值≈1~5）。2026-10-14 实测：
                            # 不合并这条 ⇒ 22 轮跑完统计到的输出只有 107 token（明显偏低）。
                            usage = event.get("usage") or {}
                            if usage:
                                merged = dict(result.usage or {})
                                merged.update(usage)
                                result.usage = merged
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
        _normalize_usage(result)
        return result


def _normalize_usage(result: LLMResult) -> None:
    """把 anthropic 的 usage 字段名映射成 agent 记账认的那套（2026-10-14）。

    ``agent._account_usage`` 只读 ``prompt_tokens`` / ``completion_tokens`` /
    ``prompt_tokens_details.cached_tokens``，而 anthropic 返回的是
    ``input_tokens`` / ``output_tokens`` / ``cache_read_input_tokens`` —— 不映射
    的话 session.json 的 ``usage_total`` 永远是 0（实测：22 轮跑完记 0）。
    anthropic 的输入侧总量 = input_tokens + cache_read + cache_creation。
    """
    usage = result.usage or {}
    if not usage:
        return
    cached = int(usage.get("cache_read_input_tokens") or 0)
    total_in = (int(usage.get("input_tokens") or 0) + cached
                + int(usage.get("cache_creation_input_tokens") or 0))
    usage.setdefault("prompt_tokens", total_in)
    usage.setdefault("completion_tokens",
                     int(usage.get("output_tokens") or 0))
    usage.setdefault("prompt_tokens_details", {"cached_tokens": cached})
    result.usage = usage
