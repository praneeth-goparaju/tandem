from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from src.brain.mcp.caller_context import ToolCaller, caller_scope


def _propose(area: str, *, title: str) -> str:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    with caller_scope(ToolCaller(
        agent=area,
        area=area,
        allowed_action_types=frozenset({"push_notification"}),
    )):
        asyncio.run(
            propose_action(
                {
                    "type": "push_notification",
                    "payload": {
                        "title": title,
                        "message": "body",
                        "agent": "spoofed-agent",
                        "area": "spoofed-area",
                    },
                    "rationale": "security test",
                    "risk": "confirm",
                }
            )
        )
    return queue.list_by_status("pending", origin_area=area)[0]["id"]


def test_proposal_persists_immutable_caller_and_payload_provenance(isolated_db) -> None:
    from src.brain.actions import queue

    action_id = _propose("family", title="Family")
    row = queue.get(action_id)

    assert row["origin_agent"] == "family"
    assert row["origin_area"] == "family"
    assert row["approval_audience"] == "area:family"
    payload = json.loads(row["payload_json"])
    assert payload["agent"] == "family"
    assert payload["area"] == "family"
    assert row["version"] == 1
    assert len(row["payload_hash"]) == 64


def test_pi_bridge_injects_caller_context_outside_model_arguments(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.pi_bridge import _call

    result = asyncio.run(_call({
        "name": "propose_action",
        "allowed": ["propose_action"],
        "caller_context": {
            "agent": "health",
            "area": "health",
            "allowed_action_types": ["push_notification"],
            "is_reply": False,
            "untrusted_input": False,
        },
        "arguments": {
            "type": "push_notification",
            "payload": {"title": "Health", "message": "body"},
            "rationale": "bridge provenance test",
            "risk": "confirm",
        },
    }))

    assert result["is_error"] is False
    row = queue.list_by_status("pending")[0]
    assert row["origin_agent"] == "health"
    assert row["origin_area"] == "health"
    assert len(row["dedupe_key"]) == 64


def test_pending_listing_is_scoped_to_trusted_caller_area(isolated_db) -> None:
    from src.brain.mcp.tools.actions import list_pending_actions

    family_id = _propose("family", title="Family")
    career_id = _propose("career", title="Career")

    with caller_scope(ToolCaller(agent="family", area="family")):
        text = asyncio.run(list_pending_actions({"status": "pending"}))["content"][0][
            "text"
        ]

    assert family_id in text
    assert career_id not in text


def test_text_approval_cannot_cross_action_origin_area(isolated_db) -> None:
    from src.brain.actions import approvals, queue

    action_id = _propose("family", title="Family")
    proposed = queue.get(action_id)
    queue.record_review_delivery(
        action_id, proposed["payload_hash"], "family", "CFAMILY", "1.1"
    )

    assert approvals.handle_reply(
        f"yes {action_id}", area="career", actor="UOWNER", owner=True
    )
    assert queue.get(action_id)["status"] == "pending"

    assert approvals.handle_reply(
        f"yes {action_id}", area="family", actor="UOWNER", owner=True
    )
    row = queue.get(action_id)
    assert row["status"] == "approved"
    assert row["approval_actor"] == "UOWNER"


def test_legacy_unscoped_action_is_owner_only(isolated_db) -> None:
    from src.brain.actions import approvals, queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(
            Action(
                id="abc123def456",
                type=ActionType.PUSH_NOTIFICATION,
                payload={"title": "Legacy", "message": "Review me"},
                rationale="legacy",
            )
    )
    approvals.handle_reply(
        "yes abc123def456", area=None, actor="UCO", owner=False
    )
    assert queue.get("abc123def456")["status"] == "pending"
    approvals.handle_reply(
        "yes abc123def456", area=None, actor="UOWNER", owner=True
    )
    assert queue.get("abc123def456")["status"] == "approved"


def test_push_wrapper_ignores_model_authored_route(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.notifications import push_notify

    fake_notifier = MagicMock()
    fake_notifier.send = AsyncMock(return_value={"status": "noop"})
    fake_notifier.persona_for = lambda *_args, **_kwargs: None
    with (
        patch(
            "src.brain.actions.handlers.push_notification.notifier",
            return_value=fake_notifier,
        ),
        caller_scope(ToolCaller(
            agent="health",
            area="health",
            allowed_action_types=frozenset({"push_notification"}),
        )),
    ):
        asyncio.run(push_notify({
            "title": "Health",
            "message": "Body",
            "area": "career",
            "agent": "career",
        }))

    row = queue.list_by_status("executed")[0]
    payload = json.loads(row["payload_json"])
    assert (payload["agent"], payload["area"]) == ("health", "health")


def test_direct_reply_ignores_model_authored_route(isolated_db) -> None:
    from src.brain.mcp.tools.replies import reply_to_message

    fake_notifier = MagicMock()
    fake_notifier.send = AsyncMock(return_value={
        "status": "sent",
        "id": "123.456",
        "group_id": "CHEALTH",
    })
    with (
        patch("src.brain.mcp._common.notifier", return_value=fake_notifier),
        caller_scope(ToolCaller(agent="health", area="health", is_reply=True)),
    ):
        asyncio.run(reply_to_message({
            "text": "Body",
            "area": "career",
            "agent": "career",
            "proposal_action_ids": ["abc123def456"],
        }))

    call = fake_notifier.send.await_args
    assert call.kwargs["agent"] == "health"
    assert call.kwargs["area"] == "health"
    assert call.kwargs["proposal_action_ids"] == ["abc123def456"]


def test_direct_reply_refuses_scheduled_caller(isolated_db) -> None:
    from src.brain.mcp.tools.replies import reply_to_message

    fake_notifier = MagicMock()
    fake_notifier.send = AsyncMock(return_value={"status": "sent", "id": "123.456"})
    with (
        patch("src.brain.mcp._common.notifier", return_value=fake_notifier),
        caller_scope(ToolCaller(agent="health", area="health", is_reply=False)),
    ):
        result = asyncio.run(reply_to_message({"text": "Body"}))

    assert "ERROR:" in result["content"][0]["text"]
    fake_notifier.send.assert_not_awaited()
