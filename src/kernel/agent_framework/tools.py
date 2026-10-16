"""Tool registry — how tools are registered and their schemas built.

Registration can supply ``description``/``parameters`` explicitly or let the
registry derive them from the handler docstring + signature (annotations →
JSON type, missing defaults → required).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import typing
from dataclasses import dataclass
from types import UnionType
from typing import Any, Callable, Optional, Union

from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.tools")

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PY_TO_JSON_TYPE = {int: "integer", float: "number", str: "string",
                    bool: "boolean"}


def _resolve_ptype(ptype: Any) -> tuple[Any, bool]:
    base = getattr(ptype, "__origin__", None)
    args = getattr(ptype, "__args__", None)
    if base in (Union, UnionType) or isinstance(ptype, UnionType):
        non_none = [a for a in (args or ()) if a is not type(None)]
        if len(non_none) < len(args or ()):
            return (non_none[0] if non_none else str, True)
    return ptype, False


def schema_from_handler(name: str, handler: Callable[..., Any]) -> dict[str, Any]:
    """Build an OpenAI function tool definition from the handler signature
    and docstring."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        signature = None
    try:
        hints = typing.get_type_hints(handler)
    except Exception:  # noqa: BLE001
        hints = {}
    if signature is not None:
        for pname, param in signature.parameters.items():
            if pname in ("self", "return"):
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD):
                continue
            ptype = hints.get(pname, param.annotation)
            ptype, optional = _resolve_ptype(ptype)
            if ptype is list or getattr(ptype, "__origin__", None) is list:
                schema_type = "array"
            else:
                schema_type = _PY_TO_JSON_TYPE.get(ptype, "string")
            properties[pname] = {"type": schema_type}
            if param.default is inspect.Parameter.empty and not optional:
                required.append(pname)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": inspect.getdoc(handler) or "",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


@dataclass(slots=True)
class ToolSpec:
    name: str
    handler: Callable[..., Any]
    description: str
    parameters: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        description: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
    ) -> None:
        if not _TOOL_NAME.fullmatch(name or ""):
            raise ValueError(
                "tool name must match ^[A-Za-z0-9_-]{1,64}$")
        if not callable(handler):
            raise ValueError("callable handler is required")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if description is None or parameters is None:
            generated = schema_from_handler(name, handler)
            if description is None:
                description = generated["function"]["description"]
            if parameters is None:
                parameters = generated["function"]["parameters"]
        self._tools[name] = ToolSpec(
            name=name, handler=handler,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
        )
        logger.debug("tool registered name=%s", name)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in sorted(self._tools)]

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        handler = self.get(name).handler
        if inspect.iscoroutinefunction(handler):
            return await handler(**params)
        return await asyncio.to_thread(handler, **params)
