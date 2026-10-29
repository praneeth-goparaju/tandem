from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.brain.mcp.pi_bridge import _call

_CALLER_CONTEXT = {
    "agent": "test",
    "area": "test",
    "allowed_action_types": [],
    "direct_action_types": [],
    "is_reply": False,
    "untrusted_input": False,
}


def test_bridge_rejects_tool_outside_per_call_allowlist():
    result = asyncio.run(_call({
        "name": "store_finding",
        "allowed": [],
        "arguments": {},
    }))
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "tool not allowed"


def test_bridge_rejects_registered_tool_without_trusted_caller_context():
    result = asyncio.run(_call({
        "name": "store_finding",
        "allowed": ["store_finding"],
        "arguments": {},
    }))

    assert result["is_error"] is True
    assert result["content"][0]["text"] == "valid caller_context is required"


@pytest.mark.parametrize(
    "caller_context",
    [
        {"agent": "health", "area": "health"},
        {"agent": "health", "area": "health", "allowed_action_types": "*"},
        {"agent": "health", "area": "health", "allowed_action_types": [7]},
        {
            "agent": "health", "area": "health", "allowed_action_types": [],
            "is_reply": False, "untrusted_input": "no",
        },
        {
            "agent": "health", "area": "health",
            "allowed_action_types": ["mail_archive"],
            "direct_action_types": "mail_archive",
            "is_reply": False, "untrusted_input": False,
        },
        {
            "agent": "health", "area": "health", "allowed_action_types": [],
            "direct_action_types": ["mail_archive"],
            "is_reply": False, "untrusted_input": False,
        },
    ],
)
def test_bridge_rejects_malformed_action_capability_envelope(caller_context):
    result = asyncio.run(_call({
        "name": "store_finding",
        "allowed": ["store_finding"],
        "caller_context": caller_context,
        "arguments": {},
    }))

    assert result["is_error"] is True
    assert result["content"][0]["text"] == "valid caller_context is required"


def _run_handler(monkeypatch, result):
    async def handler(_args):
        return result

    monkeypatch.setattr(
        "src.brain.mcp.pi_bridge._load_specs",
        lambda: {"sample": SimpleNamespace(handler=handler)},
    )
    return asyncio.run(_call({
        "name": "sample",
        "allowed": ["sample"],
        "caller_context": _CALLER_CONTEXT,
        "arguments": {},
    }))


def test_bridge_propagates_native_handler_error(monkeypatch):
    result = _run_handler(monkeypatch, {
        "is_error": True,
        "content": [{"type": "text", "text": "ERROR: invalid input"}],
    })

    assert result["is_error"] is True


def test_bridge_normalizes_legacy_error_prefix(monkeypatch):
    result = _run_handler(monkeypatch, {
        "content": [{"type": "text", "text": "  ERROR: old handler failed"}],
    })

    assert result["is_error"] is True


def test_bridge_preserves_success_status(monkeypatch):
    result = _run_handler(monkeypatch, {
        "content": [{"type": "text", "text": "operation completed"}],
    })

    assert result["is_error"] is False
    assert _run_handler(monkeypatch, "plain text") == {
        "is_error": False, "content": [{"type": "text", "text": "plain text"}],
    }
