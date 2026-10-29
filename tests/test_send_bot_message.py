from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock, patch


class FakeNotifier:
    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.lookups: list[tuple[str | None, bool]] = []
        self.sends: list[tuple[str, dict]] = []

    def channel_for_area(self, area: str | None, *, allow_default: bool = True) -> str | None:
        self.lookups.append((area, allow_default))
        return self.routes.get(str(area))

    async def send(self, message: str, **kwargs):
        self.sends.append((message, kwargs))
        return {
            "status": "sent",
            "id": "1720000000.000100",
            "group_id": "CNOTEXPOSED",
        }


def _trusted_call(arguments: dict) -> dict:
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.notifications import send_bot_message

    caller = ToolCaller(
        agent="external-mcp",
        area="interactive-mcp",
        allowed_action_types=frozenset({ActionType.SEND_BOT_MESSAGE.value}),
        direct_action_types=frozenset({ActionType.SEND_BOT_MESSAGE.value}),
        is_reply=False,
        untrusted_input=False,
    )
    with caller_scope(caller):
        return asyncio.run(send_bot_message(arguments))


def test_trusted_send_executes_once_through_configured_bot_route(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.handlers.send_bot_message import _delivery_key

    fake = FakeNotifier({"health": "CHEALTH"})
    audit = Mock()
    with (
        patch(
            "src.brain.actions.handlers.send_bot_message.notifier",
            return_value=fake,
        ),
        patch(
            "src.brain.actions.handlers.send_bot_message.record_notification",
            audit,
        ),
    ):
        arguments = {
            "area": "health",
            "message": "  Workout logged\n",
            "title": " Done ",
        }
        result = _trusted_call(arguments)
        duplicate = _trusted_call({"area": "health", "message": "Workout logged", "title": "Done"})
        control_duplicate = _trusted_call(
            {
                "area": "health",
                "message": "Workout\u0000 logged",
                "title": "Done\u0000",
            }
        )

    text = result["content"][0]["text"]
    assert "executed" in text
    assert "already executed" in duplicate["content"][0]["text"]
    assert "already executed" in control_duplicate["content"][0]["text"]
    rows = queue.list_by_status("executed")
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "send_bot_message"
    assert row["approval_channel"] == "trusted_client"
    assert row["approval_actor"] == "external-mcp"
    assert row["origin_area"] == "interactive-mcp"
    assert json.loads(row["payload_json"]) == {
        "destination_area": "health",
        "message": "Workout logged",
        "title": "Done",
    }

    assert fake.lookups == [("health", False)]
    assert len(fake.sends) == 1
    message, kwargs = fake.sends[0]
    assert message == "Workout logged"
    assert kwargs["area"] == "health"
    assert kwargs["agent"] == "external-mcp"
    assert kwargs["title"] == "Done"
    assert kwargs["plain_text"] is True
    assert kwargs["require_explicit_area"] is True
    assert kwargs["proposal_action_ids"] == []
    assert kwargs["finding_id"] is None
    assert kwargs["idempotency_key"] == _delivery_key(row["id"])
    assert "quote_timestamp" not in kwargs
    assert "attachments" not in kwargs
    audit.assert_called_once()
    assert audit.call_args.kwargs["agent"] == "external-mcp"
    assert audit.call_args.kwargs["area"] == "health"


def test_unknown_area_fails_without_a_slack_call(isolated_db) -> None:
    from src.brain.actions import queue

    fake = FakeNotifier({})
    with patch("src.brain.actions.handlers.send_bot_message.notifier", return_value=fake):
        result = _trusted_call({"area": "unknown", "message": "Hello"})

    assert "FAILED" in result["content"][0]["text"]
    rows = queue.list_by_status("failed")
    assert len(rows) == 1
    assert "no configured Brain Slack route" in rows[0]["result"]
    assert fake.lookups == [("unknown", False)]
    assert fake.sends == []


def test_untrusted_or_non_direct_caller_cannot_queue_a_send(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.notifications import send_bot_message

    caller = ToolCaller(
        agent="scheduled-agent",
        area="health",
        allowed_action_types=frozenset({ActionType.SEND_BOT_MESSAGE.value}),
        direct_action_types=frozenset(),
        is_reply=False,
        untrusted_input=False,
    )
    with caller_scope(caller):
        result = asyncio.run(send_bot_message({"area": "health", "message": "Hello"}))

    assert result["is_error"] is True
    assert "trusted direct MCP capability" in result["content"][0]["text"]
    assert queue.list_by_status("pending") == []


def test_direct_grant_is_bound_to_the_external_gateway_identity(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.notifications import send_bot_message

    action_type = ActionType.SEND_BOT_MESSAGE.value
    caller = ToolCaller(
        agent="lookalike-client",
        area="interactive-mcp",
        allowed_action_types=frozenset({action_type}),
        direct_action_types=frozenset({action_type}),
        is_reply=False,
        untrusted_input=False,
    )
    with caller_scope(caller):
        result = asyncio.run(send_bot_message({"area": "health", "message": "Hello"}))

    assert result["is_error"] is True
    assert queue.list_by_status("pending") == []


def test_payload_bounds_fail_before_notifier_dispatch(isolated_db) -> None:
    from src.brain.actions import queue

    fake = FakeNotifier({"health": "CHEALTH"})
    with patch("src.brain.actions.handlers.send_bot_message.notifier", return_value=fake):
        result = _trusted_call({"area": "health", "message": "x" * 4_001})

    assert "at most 4000" in result["content"][0]["text"]
    assert queue.list_by_status("pending") == []
    assert fake.sends == []


def test_provider_diagnostics_are_not_returned_to_the_client(isolated_db) -> None:
    from src.brain.actions import queue

    class FailingNotifier(FakeNotifier):
        async def send(self, message: str, **kwargs):
            raise RuntimeError("xoxb-secret channel=C123 private/path")

    fake = FailingNotifier({"health": "CHEALTH"})
    with patch("src.brain.actions.handlers.send_bot_message.notifier", return_value=fake):
        result = _trusted_call({"area": "health", "message": "Hello"})

    text = result["content"][0]["text"]
    row = queue.list_by_status("failed")[0]
    assert "Slack bot delivery failed" in text
    assert row["result"] == "Slack bot delivery failed"
    for secret in ("xoxb-secret", "C123", "private/path"):
        assert secret not in text
        assert secret not in row["result"]


def test_tool_schema_has_only_bounded_route_and_content_inputs() -> None:
    from src.brain.mcp import tool_registry
    from src.brain.mcp.tools import notifications as _notifications  # noqa: F401

    spec = next(tool for tool in tool_registry.all_tools() if tool.name == "send_bot_message")
    schema = spec.input_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["area", "message"]
    assert set(schema["properties"]) == {"area", "message", "title"}
    assert schema["properties"]["area"]["maxLength"] == 64
    assert schema["properties"]["message"]["maxLength"] == 4_000
    assert schema["properties"]["title"]["maxLength"] == 120
