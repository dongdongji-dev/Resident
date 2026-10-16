"""Provider-neutral LLM protocol + shared streaming retry (exponential
backoff)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Tuple, Type


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str = "{}"
    # Provider-opaque blob that must be echoed back verbatim on the next turn
    # (Gemini 4 requires the ``thoughtSignature`` of a functionCall part;
    # omitting it → 400 "Function call is missing a thought_signature").
    signature: str = ""

    @property
    def params(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(slots=True)
class LLMResult:
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Optional[dict[str, Any]] = None
    # Raw provider output items (Responses API). Chat-compatible leaves empty.
    provider_items: list[dict[str, Any]] = field(default_factory=list)


DeltaCallback = Callable[[str, str], None]


class LLMClient(Protocol):
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Optional[DeltaCallback] = None,
    ) -> LLMResult:
        """Stream one completion and return its collected result."""


def merge_options(options: Optional[dict[str, Any]],
                  extra_params: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged = dict(options or {})
    merged.update(extra_params or {})
    return merged


Retryable = Tuple[Type[BaseException], ...]


def retry_sleep(attempt: int, base_delay: float,
                backoff_base: float = 2.0, cap_seconds: float = 60.0) -> float:
    """Exponential backoff: ``base_delay * backoff_base**attempt``, capped."""
    return min(base_delay * (backoff_base ** attempt), cap_seconds)


async def stream_with_retry(
    attempt: Callable[[], Awaitable["LLMResult"]],
    *,
    retries: int,
    retry_delay_seconds: float,
    retryable: Retryable,
    backoff_base: float = 2.0,
    cap_seconds: float = 60.0,
    description: str = "LLM stream",
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> "LLMResult":
    """Run ``attempt()`` up to ``retries+1`` times with exponential backoff.

    Only exceptions in ``retryable`` are retried; everything else propagates
    immediately. The first attempt waits nothing; the nth retry sleeps
    ``retry_sleep`` before the next attempt. When all attempts fail, a
    ``RuntimeError`` is raised (never a raw network exception).
    """
    last: Optional[BaseException] = None
    for i in range(max(0, int(retries)) + 1):
        try:
            return await attempt()
        except retryable as exc:  # noqa: BLE001
            last = exc
            if i < int(retries):
                if on_retry is not None:
                    try:
                        on_retry(i + 1, exc)
                    except Exception:  # noqa: BLE001 — telemetry never kills
                        pass
                await asyncio.sleep(
                    retry_sleep(i, float(retry_delay_seconds),
                                backoff_base, cap_seconds))
    raise RuntimeError(
        f"{description} failed after {int(retries) + 1} attempts: {last}") from last
