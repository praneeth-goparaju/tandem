from __future__ import annotations

import io
import json

from src.gateway.protocol import JsonRpcServer, serve_lines


class FakeService:
    def list_tools(self):
        return [{"name": "mail_search", "description": "Search", "inputSchema": {}}]

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": f"{name}:{arguments}"}]}


def _server(logs=None):
    target = logs if logs is not None else []
    return JsonRpcServer(
        FakeService(),
        server_name="brain-tools",
        server_version="test",
        log=target.append,
    )


def _modern_meta(*, version="2026-07-28", client_info=True):
    meta = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if client_info:
        meta["io.modelcontextprotocol/clientInfo"] = {
            "name": "test-client",
            "version": "1.0.0",
        }
    return meta


def test_initialize_negotiates_known_version() -> None:
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )

    assert response["result"]["protocolVersion"] == "2025-03-26"
    assert response["result"]["serverInfo"]["name"] == "brain-tools"
    assert "iCloud Mail" in response["result"]["instructions"]
    assert "search or list Brain tools" in response["result"]["instructions"]


def test_initialize_falls_back_to_latest_legacy_version() -> None:
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2026-07-28"},
        }
    )

    assert response["result"]["protocolVersion"] == "2025-11-25"


def test_modern_discovery_and_tool_list_have_required_metadata() -> None:
    server = _server()
    discover = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": _modern_meta()},
        }
    )
    tools = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": _modern_meta(client_info=False)},
        }
    )

    assert discover["result"]["supportedVersions"] == ["2026-07-28"]
    assert discover["result"]["resultType"] == "complete"
    assert discover["result"]["cacheScope"] == "private"
    assert "Apple Reminders" in discover["result"]["instructions"]
    assert "search or list Brain tools" in discover["result"]["instructions"]
    assert discover["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == (
        "brain-tools"
    )
    assert tools["result"]["resultType"] == "complete"
    assert tools["result"]["ttlMs"] == 300_000
    assert tools["result"]["cacheScope"] == "private"


def test_unknown_modern_protocol_returns_supported_versions() -> None:
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": _modern_meta(version="2099-01-01")},
        }
    )

    assert response["error"]["code"] == -32022
    assert response["error"]["data"]["requested"] == "2099-01-01"
    assert "2026-07-28" in response["error"]["data"]["supported"]


def test_modern_discovery_requires_metadata() -> None:
    response = _server().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
    )

    assert response["error"]["code"] == -32602
    assert "requires protocol" in response["error"]["message"]


def test_modern_request_requires_client_capabilities() -> None:
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        }
    )

    assert response["error"]["code"] == -32602
    assert "clientCapabilities" in response["error"]["message"]


def test_modern_request_rejects_malformed_client_info() -> None:
    meta = _modern_meta()
    meta["io.modelcontextprotocol/clientInfo"] = {"name": "test-client"}
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": meta},
        }
    )

    assert response["error"]["code"] == -32602
    assert "clientInfo.version" in response["error"]["message"]


def test_notifications_have_no_response() -> None:
    assert (
        _server().handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        is None
    )


def test_unknown_method_uses_json_rpc_error() -> None:
    response = _server().handle({"jsonrpc": "2.0", "id": "x", "method": "unknown", "params": {}})

    assert response["error"]["code"] == -32601


def test_serve_lines_frames_responses_and_parse_errors() -> None:
    requests = b"\n".join(
        [
            b"not-json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode(),
            b"",
        ]
    )
    output = io.BytesIO()

    serve_lines(io.BytesIO(requests), output, _server(), max_request_bytes=4096)

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"]["tools"][0]["name"] == "mail_search"


def test_serve_lines_rejects_and_drains_oversized_request() -> None:
    oversized = b"{" + (b"x" * 5000) + b"}\n"
    valid = (
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}).encode()
        + b"\n"
    )
    output = io.BytesIO()

    serve_lines(io.BytesIO(oversized + valid), output, _server(), max_request_bytes=1024)

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["message"] == "request exceeds size limit"
    assert responses[1]["id"] == 4


def test_internal_failures_are_not_returned_to_client() -> None:
    logs = []

    class BrokenService(FakeService):
        def list_tools(self):
            raise RuntimeError("private path and diagnostic")

    server = JsonRpcServer(
        BrokenService(), server_name="brain-tools", server_version="test", log=logs.append
    )
    response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})

    assert response["error"] == {"code": -32603, "message": "internal server error"}
    assert "private path" in logs[0]
