from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


_REGISTRY: list[ToolSpec] = []


def brain_tool(name: str, description: str, input_schema: dict[str, Any]):
    def decorator(handler: ToolHandler) -> ToolHandler:
        if any(t.name == name for t in _REGISTRY):
            raise ValueError(f"brain_tool: duplicate registration for {name!r}")
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )
        _REGISTRY.append(spec)
        return handler
    return decorator


def all_tools() -> list[ToolSpec]:
    return list(_REGISTRY)
