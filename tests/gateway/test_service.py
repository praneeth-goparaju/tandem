from __future__ import annotations

import pytest

from src.gateway.contracts import SEND_BOT_MESSAGE_SCHEMA
from src.gateway.profiles import load_profile
from src.gateway.service import CatalogError, ToolService


class FakeBackend:
    def __init__(self, catalog, result=None):
        self.catalog = catalog
        self.result = result or {"content": [{"type": "text", "text": "ok"}]}
        self.calls = []

    def invoke(self, operation, payload=None):
        self.calls.append((operation, payload))
        return self.catalog if operation == "list" else self.result


def _read_catalog():
    profile = load_profile({})
    return [
        {"name": name, "description": f"Description for {name}", "input_schema": {}}
        for name in sorted(profile.tool_names)
    ]


def test_catalog_is_filtered_sorted_and_cached() -> None:
    backend = FakeBackend(
        _read_catalog() + [{"name": "push_notify", "description": "hidden", "input_schema": {}}]
    )
    service = ToolService(backend, load_profile({}))

    first = service.list_tools()
    second = service.list_tools()

    assert len(first) == 24
    assert [tool["name"] for tool in first] == sorted(tool["name"] for tool in first)
    assert first == second
    assert backend.calls == [("list", None)]


def test_hidden_tool_is_rejected_without_backend_call() -> None:
    backend = FakeBackend(_read_catalog())
    service = ToolService(backend, load_profile({}))

    result = service.call_tool("push_notify", {})

    assert result["isError"] is True
    assert backend.calls == []


def test_call_injects_server_owned_provenance() -> None:
    backend = FakeBackend(_read_catalog())
    service = ToolService(backend, load_profile({}))

    result = service.call_tool("mail_search", {"query": "hello"})

    assert result["isError"] is False
    operation, payload = backend.calls[-1]
    assert operation == "call"
    assert payload["allowed"] == ["mail_search"]
    assert payload["caller_context"]["agent"] == "external-mcp-read"
    assert payload["arguments"] == {"query": "hello"}


def test_invalid_arguments_never_reach_backend() -> None:
    catalog = _read_catalog()
    mail_search = next(tool for tool in catalog if tool["name"] == "mail_search")
    mail_search["input_schema"] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    backend = FakeBackend(catalog)
    service = ToolService(backend, load_profile({}))

    result = service.call_tool("mail_search", {})

    assert result["isError"] is True
    assert "required" in result["content"][0]["text"]
    assert backend.calls == [("list", None)]


def test_missing_profile_tool_fails_strict_catalog() -> None:
    service = ToolService(FakeBackend([]), load_profile({}))

    with pytest.raises(CatalogError, match="missing configured tools"):
        service.list_tools()


def test_exact_trusted_catalog_and_direct_action_context() -> None:
    profile = load_profile({"BRAIN_MCP_PROFILE": "trusted", "BRAIN_MCP_ALLOW_WRITES": "1"})
    expected = set(profile.tool_names)
    catalog = [
        {"name": name, "description": name, "input_schema": {"type": "object"}} for name in expected
    ]
    backend = FakeBackend(catalog)
    service = ToolService(backend, profile)

    assert {tool["name"] for tool in service.list_tools()} == expected
    service.call_tool(
        "send_bot_message",
        {"area": "health", "message": "A user-authorized test message"},
    )
    _, payload = backend.calls[-1]
    assert payload["allowed"] == ["send_bot_message"]
    assert payload["caller_context"]["direct_action_types"] == [
        "complete_reminder",
        "mail_archive",
        "mail_archive_batch",
        "send_bot_message",
    ]
    assert "complete_reminder" in payload["caller_context"]["allowed_action_types"]
    assert "complete_reminder" in payload["caller_context"]["direct_action_types"]
    assert payload["caller_context"]["is_reply"] is False
    assert payload["caller_context"]["untrusted_input"] is False


def test_bot_message_rejects_raw_slack_destination_before_backend_call() -> None:
    profile = load_profile({"BRAIN_MCP_PROFILE": "trusted", "BRAIN_MCP_ALLOW_WRITES": "1"})
    catalog = [
        {"name": name, "description": name, "input_schema": {"type": "object"}}
        for name in profile.tool_names
    ]
    bot_tool = next(tool for tool in catalog if tool["name"] == "send_bot_message")
    bot_tool["input_schema"] = {
        "type": "object",
        "properties": {"channel": {"type": "string"}},
    }
    backend = FakeBackend(catalog)
    service = ToolService(backend, profile)

    public_tool = next(tool for tool in service.list_tools() if tool["name"] == "send_bot_message")
    assert public_tool["inputSchema"] == SEND_BOT_MESSAGE_SCHEMA

    result = service.call_tool(
        "send_bot_message",
        {"area": "health", "message": "hello", "channel": "C123"},
    )

    assert result["isError"] is True
    assert "not an allowed property" in result["content"][0]["text"]
    assert backend.calls == [("list", None)]
