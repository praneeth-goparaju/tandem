"""Small, dependency-free MCP stdio protocol implementation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from src.gateway.service import ToolService

MODERN_PROTOCOL_VERSION = "2026-07-28"
LATEST_LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_LEGACY_PROTOCOL_VERSIONS = frozenset(
    {LATEST_LEGACY_PROTOCOL_VERSION, "2025-06-18", "2025-03-26"}
)
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

SERVER_INSTRUCTIONS = (
    "Brain tools provide the signed-in local user's iCloud Mail and Gmail "
    "search, read, attachment, and archive workflows; iCloud Calendar event "
    "read, create, and update workflows; Apple Reminders read and create "
    "workflows; iCloud Contacts search and read; Garmin health data; the people "
    "diary; findings; and selected local workflows. Clients may defer individual "
    "tool definitions, so search or list Brain tools before concluding that one "
    "of these capabilities is unavailable. Respect each tool's read or write "
    "semantics and use the published Brain tools only for the signed-in local user."
)


@dataclass(frozen=True)
class RpcError(Exception):
    code: int
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class JsonRpcServer:
    def __init__(
        self,
        service: ToolService,
        *,
        server_name: str,
        server_version: str,
        log: Callable[[str], None],
    ):
        self.service = service
        self.server_name = server_name
        self.server_version = server_version
        self.log = log

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, Mapping):
            return _error_response(None, -32600, "invalid JSON-RPC request")
        request_id = message.get("id")
        has_id = "id" in message
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error_response(request_id if has_id else None, -32600, "invalid request")

        method = str(message["method"])
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            if not has_id:
                return None
            return _error_response(request_id, -32602, "params must be an object")

        try:
            requested_protocol = _request_protocol(params)
        except RpcError as exc:
            if not has_id:
                return None
            return _error_response(request_id, exc.code, exc.message, data=exc.data)
        if requested_protocol is not None and requested_protocol not in {
            MODERN_PROTOCOL_VERSION,
            *SUPPORTED_LEGACY_PROTOCOL_VERSIONS,
        }:
            if not has_id:
                return None
            return _error_response(
                request_id,
                -32022,
                "unsupported protocol version",
                data={
                    "requested": requested_protocol,
                    "supported": [
                        MODERN_PROTOCOL_VERSION,
                        *sorted(SUPPORTED_LEGACY_PROTOCOL_VERSIONS, reverse=True),
                    ],
                },
            )

        modern = requested_protocol == MODERN_PROTOCOL_VERSION or method == "server/discover"
        try:
            if method == "server/discover" and requested_protocol != MODERN_PROTOCOL_VERSION:
                raise RpcError(
                    -32602,
                    f"server/discover requires protocol {MODERN_PROTOCOL_VERSION}",
                )
            if modern:
                _validate_modern_metadata(params)
            result = self._dispatch(method, params)
        except RpcError as exc:
            if not has_id:
                return None
            return _error_response(request_id, exc.code, exc.message, data=exc.data)
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            self.log(f"{method} failed: {type(exc).__name__}: {exc}")
            if not has_id:
                return None
            return _error_response(request_id, -32603, "internal server error")

        if not has_id:
            return None
        if modern and method != "server/discover":
            result = self._modern_result(method, result)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = (
                requested
                if isinstance(requested, str) and requested in SUPPORTED_LEGACY_PROTOCOL_VERSIONS
                else LATEST_LEGACY_PROTOCOL_VERSION
            )
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": self.server_version},
                "instructions": SERVER_INSTRUCTIONS,
            }
        if method == "server/discover":
            return {
                "resultType": "complete",
                "supportedVersions": [MODERN_PROTOCOL_VERSION],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": SERVER_INSTRUCTIONS,
                "ttlMs": 300_000,
                "cacheScope": "private",
                "_meta": {SERVER_INFO_META_KEY: self._server_info()},
            }
        if method == "ping":
            if _request_protocol(params) == MODERN_PROTOCOL_VERSION:
                raise RpcError(-32601, "method not found: ping")
            return {}
        if method == "tools/list":
            return {"tools": self.service.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise RpcError(-32602, "tool name must be a non-empty string")
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "tool arguments must be an object")
            return self.service.call_tool(name, arguments)
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return {}
        raise RpcError(-32601, f"method not found: {method}")

    def _modern_result(self, method: str, result: dict[str, Any]) -> dict[str, Any]:
        modern = dict(result)
        modern["resultType"] = "complete"
        modern["_meta"] = {SERVER_INFO_META_KEY: self._server_info()}
        if method == "tools/list":
            modern["ttlMs"] = 300_000
            modern["cacheScope"] = "private"
        return modern

    def _server_info(self) -> dict[str, str]:
        return {"name": self.server_name, "version": self.server_version}


def serve_lines(
    reader: BinaryIO,
    writer: BinaryIO,
    server: JsonRpcServer,
    *,
    max_request_bytes: int,
) -> None:
    """Serve newline-delimited JSON-RPC until stdin closes."""
    while raw_line := reader.readline(max_request_bytes + 1):
        if not raw_line.strip():
            continue
        if len(raw_line) > max_request_bytes:
            while raw_line and not raw_line.endswith(b"\n"):
                raw_line = reader.readline(max_request_bytes + 1)
            _write(writer, _error_response(None, -32700, "request exceeds size limit"))
            continue
        try:
            message = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(writer, _error_response(None, -32700, "parse error"))
            continue
        response = server.handle(message)
        if response is not None:
            _write(writer, response)


def _write(writer: BinaryIO, message: Mapping[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    writer.write(payload + b"\n")
    writer.flush()


def _request_protocol(params: Mapping[str, Any]) -> str | None:
    meta = params.get("_meta")
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        raise RpcError(-32602, "_meta must be an object")
    if PROTOCOL_VERSION_META_KEY not in meta:
        return None
    version = meta.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(version, str) or not version:
        raise RpcError(-32602, "protocol version metadata must be a non-empty string")
    return version


def _validate_modern_metadata(params: Mapping[str, Any]) -> None:
    meta = params.get("_meta")
    if not isinstance(meta, Mapping):
        raise RpcError(-32602, "modern MCP requests require _meta")

    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, Mapping):
        raise RpcError(-32602, "modern MCP requests require clientCapabilities")

    client_info = meta.get(CLIENT_INFO_META_KEY)
    if client_info is None:
        return
    if not isinstance(client_info, Mapping):
        raise RpcError(-32602, "clientInfo must be an object")
    for field_name in ("name", "version"):
        value = client_info.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise RpcError(
                -32602,
                f"clientInfo.{field_name} must be a non-empty string",
            )


def _error_response(
    request_id: object,
    code: int,
    message: str,
    *,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = dict(data)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }
