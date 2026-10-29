from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

NEVER_AUTO = [
    "send_draft",
    "create_contact",
    "update_contact",
    "update_calendar_event",
]


def _call(tool, **kwargs):
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    handler = getattr(tool, "handler", tool)
    caller = ToolCaller(
        agent="test",
        area="test",
        allowed_action_types=frozenset(item.value for item in ActionType),
    )
    with caller_scope(caller):
        return asyncio.run(handler(kwargs))


def _payload_for(atype: str, vault) -> dict:
    if atype == "create_contact":
        return {"full_name": "Test Person"}
    if atype == "update_contact":
        return {"href": "https://contacts.icloud.com/card.vcf", "note": "Updated"}
    if atype == "update_calendar_event":
        return {
            "event_url": "https://caldav.icloud.com/event.ics",
            "summary": "Updated event",
        }
    if atype != "send_draft":
        raise AssertionError(f"missing fixture for {atype}")
    drafts = vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    draft = drafts / "approval-clamp.md"
    draft.write_text(
        "---\nkind: message\ntitle: Clamp\nstatus: pending\n---\n\nbody\n",
        encoding="utf-8",
    )
    return {"draft_path": "00 - Inbox/drafts/approval-clamp.md"}


@pytest.mark.parametrize("atype", NEVER_AUTO)
def test_risk_auto_clamped_to_confirm_at_propose(
    isolated_db, isolated_vault, atype
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type=atype,
        payload=_payload_for(atype, isolated_vault),
        rationale="clamp test",
        risk="auto",
    )
    text = out["content"][0]["text"]
    assert "queued" in text
    pending = queue.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["risk"] == "confirm"
    assert queue.list_by_status("executed") == []


@pytest.mark.parametrize("channel", ["auto", "manual", None, "todoist"])
def test_executor_refuses_never_auto_without_human_channel(
    isolated_db, isolated_vault, channel
) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    fake = AsyncMock(return_value={"ok": True, "result": "sent"})
    with patch.dict("src.brain.actions.handlers.HANDLERS", {ActionType.SEND_DRAFT: fake}, clear=False):
        _call(
            propose_action,
            type="send_draft",
            payload=_payload_for("send_draft", isolated_vault),
            rationale="r",
            risk="confirm",
        )
        row = queue.list_by_status("pending")[0]
        queue.transition_status(row["id"], "pending", "approved", channel=channel)
        asyncio.run(executor.drain_once())

    final = queue.get(row["id"])
    assert final["status"] == "failed"
    assert "not auto-eligible" in (final["result"] or "")
    fake.assert_not_awaited()


@pytest.mark.parametrize("approval_channel", ["slack_reply", "slack_button"])
def test_executor_runs_never_auto_with_genuine_human_approval(
    isolated_db, isolated_vault, approval_channel
) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    fake = AsyncMock(return_value={"ok": True, "result": "sent"})
    with patch.dict("src.brain.actions.handlers.HANDLERS", {ActionType.SEND_DRAFT: fake}, clear=False):
        _call(
            propose_action,
            type="send_draft",
            payload=_payload_for("send_draft", isolated_vault),
            rationale="r",
            risk="confirm",
        )
        row = queue.list_by_status("pending")[0]
        queue.record_review_delivery(
            row["id"], row["payload_hash"], "test", "CTEST", "1.2"
        )
        queue.transition_status(
            row["id"],
            "pending",
            "approved",
            channel=approval_channel,
            actor="UOWNER",
        )
        asyncio.run(executor.drain_once())

    final = queue.get(row["id"])
    assert final["status"] == "executed"
    fake.assert_awaited_once()


@pytest.mark.parametrize("approval_channel", ["slack_reply", "slack_button"])
def test_executor_refuses_human_channel_without_actor_provenance(
    isolated_db, isolated_vault, approval_channel
) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    fake = AsyncMock(return_value={"ok": True, "result": "sent"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.SEND_DRAFT: fake},
        clear=False,
    ):
        _call(
            propose_action,
            type="send_draft",
            payload=_payload_for("send_draft", isolated_vault),
            rationale="r",
            risk="confirm",
        )
        row = queue.list_by_status("pending")[0]
        queue.record_review_delivery(
            row["id"], row["payload_hash"], "test", "CTEST", "1.2"
        )
        queue.transition_status(
            row["id"], "pending", "approved", channel=approval_channel
        )
        asyncio.run(executor.drain_once())

    final = queue.get(row["id"])
    assert final["status"] == "failed"
    assert "actor provenance" in (final["result"] or "")
    fake.assert_not_awaited()


def test_typed_wrapper_create_contact_auto_clamped(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.icloud_actions import _propose

    with caller_scope(ToolCaller(
        agent="test",
        area="test",
        allowed_action_types=frozenset({ActionType.CREATE_CONTACT.value}),
    )):
        asyncio.run(_propose(
            ActionType.CREATE_CONTACT,
            {"full_name": "Test"},
            {"rationale": "r", "risk": "auto"},
        ))
    pending = queue.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["risk"] == "confirm"


def test_auto_eligible_set_is_exactly_the_safe_types() -> None:
    from src.brain.actions.types import AUTO_ELIGIBLE_TYPES, ActionType

    assert frozenset({
        ActionType.PUSH_NOTIFICATION,
        ActionType.CREATE_TODOIST_TASK,
        ActionType.CREATE_REMINDER,
        ActionType.COMPLETE_REMINDER,
        ActionType.CREATE_CALENDAR_EVENT,
        ActionType.UPDATE_CALENDAR_EVENT,
    }) == AUTO_ELIGIBLE_TYPES
    for t in (ActionType.SEND_DRAFT, ActionType.CREATE_CONTACT, ActionType.UPDATE_CONTACT,
              ActionType.MAIL_ARCHIVE,
              ActionType.EDIT_VAULT_FILE,
              ActionType.PROMOTE_NOTE):
        assert t not in AUTO_ELIGIBLE_TYPES


def test_trusted_client_set_is_exactly_the_bounded_interactive_effects() -> None:
    from src.brain.actions.types import TRUSTED_CLIENT_ACTION_TYPES, ActionType

    assert TRUSTED_CLIENT_ACTION_TYPES == frozenset({
        ActionType.COMPLETE_REMINDER,
        ActionType.MAIL_ARCHIVE,
        ActionType.MAIL_ARCHIVE_BATCH,
        ActionType.SEND_BOT_MESSAGE,
    })


def test_resolve_action_not_reachable_by_agents() -> None:
    from src.brain.mcp.server import load_brain_tools

    names = {tool.name for tool in load_brain_tools()}
    assert "resolve_action" not in names
