"""Google Gemini adapter (``models/<model>:streamGenerateContent``).

Used for provider families that speak the Generative Language wire format
instead of Chat Completions / Responses / Anthropic Messages. Currently the
OpenCode Go gateway exposes Gemini only this way (2026-10-14 实测：
``/v1/chat/completions`` → ``ModelProtocolUnsupported``，而
``/v1/models/<id>:streamGenerateContent?alt=sse`` 正常)。

Wire-format deltas vs Chat Completions:
- messages → ``contents`` (role ``user`` / ``model``) plus a separate
  ``systemInstruction``;
- OpenAI ``tools`` → ``functionDeclarations``; calls come back as
  ``functionCall`` parts and results must be sent back as
  ``functionResponse`` parts under role ``user``;
- the thinking chain arrives as parts flagged ``thought: true``;
- usage lives under ``usageMetadata``.
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

logger = get_logger("kernel.agent_framework.llm.google")


def _json_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {"_raw": raw}
        return value if isinstance(value, dict) else {"_raw": raw}
    return {}


def _response_payload(content: Any) -> dict[str, Any]:
    """Wrap a tool result so it is a valid ``functionResponse.response``."""
    if isinstance(content, dict):
        return content
    return {"result": content if content is not None else ""}


def to_google_contents(
        messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                 Optional[dict[str, Any]]]:
    """Convert OpenAI-style messages into (contents, systemInstruction)."""
    contents: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "system":
            text = message.get("content")
            if text:
                system_parts.append(str(text))
            continue
        if role == "tool":
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": str(message.get("name") or "tool"),
                "response": _response_payload(message.get("content")),
            }}]})
            continue
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.get("content"):
                parts.append({"text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                part: dict[str, Any] = {"functionCall": {
                    "name": str(function.get("name") or ""),
                    "args": _json_args(function.get("arguments")),
                }}
                signature = call.get("signature")
                if signature:
                    part["thoughtSignature"] = str(signature)
                parts.append(part)
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        contents.append({"role": "user",
                         "parts": [{"text": str(message.get("content") or "")}]})
    system = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts \
        else None
    return contents, system


def to_google_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools or []:
        if str(tool.get("type") or "function") != "function":
            continue
        function = tool.get("function") or {}
        if not function.get("name"):
            continue
        declaration: dict[str, Any] = {"name": str(function["name"])}
        if function.get("description"):
            declaration["description"] = str(function["description"])
        if function.get("parameters"):
            declaration["parameters"] = function["parameters"]
        declarations.append(declaration)
    return [{"functionDeclarations": declarations}] if declarations else []


class GoogleLLM:
    """Streaming client for the Generative Language API."""

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

    @property
    def url(self) -> str:
        return (f"{self.base_url}/models/{self.model}"
                ":streamGenerateContent?alt=sse")

    def build_payload(self, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]]) -> dict[str, Any]:
        contents, system = to_google_contents(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = system
        google_tools = to_google_tools(tools)
        if google_tools:
            payload["tools"] = google_tools
        generation: dict[str, Any] = {}
        # max_tokens / temperature style knobs from config extras
        extra = dict(self.extra)
        if "max_tokens" in extra:
            generation["maxOutputTokens"] = extra.pop("max_tokens")
        if "max_completion_tokens" in extra:
            generation["maxOutputTokens"] = extra.pop("max_completion_tokens")
        if "temperature" in extra:
            generation["temperature"] = extra.pop("temperature")
        payload.update(extra)
        if generation:
            payload["generationConfig"] = generation
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
            description="google LLM",
            on_retry=lambda n, e: logger.warning(
                "google retry %d/%d: %s", n, self.retries + 1, e),
        )

    async def _stream_once(self, payload: dict[str, Any],
                           headers: dict[str, str],
                           on_delta: Optional[DeltaCallback]) -> LLMResult:
        result = LLMResult()
        calls: list[ToolCall] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.url, json=payload, headers=headers,
                                    proxy=self.proxy,
                                    ssl=self.ssl_context) as response:
                if response.status != 200:
                    body = await response.text()
                    if response.status in (408, 429) or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message=body[:300])
                    raise RuntimeError(
                        f"LLM HTTP {response.status}: {body[:300]}")
                async for raw in response.content:
                    for line in raw.decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        usage = chunk.get("usageMetadata")
                        if usage:
                            result.usage = {
                                "prompt_tokens": usage.get("promptTokenCount"),
                                "completion_tokens":
                                    usage.get("candidatesTokenCount"),
                                "total_tokens": usage.get("totalTokenCount"),
                            }
                            # Gemini 把缓存命中的输入单列，且 thinking token 算在
                            # 输出预算里（2026-10-14：单轮 32,768 里 reasoning 13,087）
                            cached = usage.get("cachedContentTokenCount")
                            if cached is not None:
                                result.usage["prompt_tokens_details"] = {
                                    "cached_tokens": int(cached or 0)}
                            thoughts = usage.get("thoughtsTokenCount")
                            if thoughts is not None:
                                result.usage["thoughts_tokens"] = int(
                                    thoughts or 0)
                        for candidate in chunk.get("candidates") or []:
                            content = candidate.get("content") or {}
                            for part in content.get("parts") or []:
                                if part.get("thought"):
                                    text = part.get("text") or ""
                                    if text:
                                        result.thinking += str(text)
                                        if on_delta:
                                            on_delta("thinking", str(text))
                                    continue
                                if "text" in part and part["text"] is not None:
                                    text = str(part["text"])
                                    if text:
                                        result.text += text
                                        if on_delta:
                                            on_delta("content", text)
                                call = part.get("functionCall")
                                if call and call.get("name"):
                                    calls.append(ToolCall(
                                        call_id=call.get("id")
                                        or f"gemini-call-{len(calls) + 1}",
                                        name=str(call["name"]),
                                        arguments=json.dumps(
                                            call.get("args") or {},
                                            ensure_ascii=False),
                                        signature=str(
                                            part.get("thoughtSignature") or ""),
                                    ))
        result.tool_calls = calls
        return result


__all__ = ["GoogleLLM", "merge_options", "to_google_contents",
           "to_google_tools"]
