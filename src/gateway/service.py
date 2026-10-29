"""Curated tool catalog and call routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol

from src.gateway.backend import BackendError
from src.gateway.contracts import PINNED_TOOL_SCHEMAS
from src.gateway.profiles import Profile
from src.gateway.validation import (
    SchemaValidationError,
    validate_arguments,
    validate_schema,
)

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class Backend(Protocol):
    def invoke(self, operation: str, payload: dict[str, Any] | None = None) -> Any: ...


class CatalogError(BackendError):
    """The backend catalog violates the MCP gateway contract."""


class ToolService:
    def __init__(self, backend: Backend, profile: Profile, *, strict_catalog: bool = True):
        self.backend = backend
        self.profile = profile
        self.strict_catalog = strict_catalog
        self._tools: tuple[dict[str, Any], ...] | None = None

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            raw_catalog = self.backend.invoke("list")
            self._tools = self._validated_catalog(raw_catalog)
        return deepcopy(list(self._tools))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.profile.tool_names:
            return _tool_error(f"tool is not exposed by the {self.profile.name} profile: {name}")

        available = {str(tool["name"]): tool for tool in self.list_tools()}
        if name not in available:
            return _tool_error(f"tool is unavailable from the configured backend: {name}")
        try:
            validate_arguments(arguments, available[name]["inputSchema"])
        except SchemaValidationError as exc:
            return _tool_error(f"invalid arguments for {name}: {exc}")

        result = self.backend.invoke(
            "call",
            {
                "name": name,
                "allowed": [name],
                "caller_context": dict(self.profile.caller_context),
                "arguments": arguments,
            },
        )
        if not isinstance(result, Mapping):
            raise BackendError("backend call result must be a JSON object")
        content = result.get("content")
        if not isinstance(content, list):
            content = [{"type": "text", "text": ""}]
        return {
            "content": content,
            "isError": bool(result.get("is_error", result.get("isError", False))),
        }

    def _validated_catalog(self, raw_catalog: object) -> tuple[dict[str, Any], ...]:
        if not isinstance(raw_catalog, list):
            raise CatalogError("backend tool catalog must be a JSON array")

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_catalog:
            if not isinstance(raw, Mapping):
                raise CatalogError("every backend tool specification must be an object")
            name = raw.get("name")
            description = raw.get("description")
            schema = raw.get("input_schema")
            if not isinstance(name, str) or not _TOOL_NAME_PATTERN.fullmatch(name):
                raise CatalogError("backend returned an invalid tool name")
            if name in seen:
                raise CatalogError(f"backend returned duplicate tool name: {name}")
            seen.add(name)
            if name not in self.profile.tool_names:
                continue
            if not isinstance(description, str) or not isinstance(schema, Mapping):
                raise CatalogError(f"backend returned an invalid specification for: {name}")
            public_schema = PINNED_TOOL_SCHEMAS.get(name, dict(schema))
            try:
                validate_schema(public_schema)
            except SchemaValidationError as exc:
                raise CatalogError(
                    f"backend returned an unsupported schema for {name}: {exc}"
                ) from exc
            selected.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": deepcopy(public_schema),
                }
            )

        missing = self.profile.tool_names - seen
        if missing and self.strict_catalog:
            missing_text = ", ".join(sorted(missing))
            raise CatalogError(f"backend is missing configured tools: {missing_text}")
        selected.sort(key=lambda tool: str(tool["name"]))
        return tuple(selected)


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
