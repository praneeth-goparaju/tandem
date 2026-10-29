from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def test_propose_then_signal_approve_then_drain_executes(isolated_db) -> None:
    from src.brain.actions import approvals, executor, queue
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import propose_action

    with caller_scope(ToolCaller(
        agent="council",
        area="council",
        allowed_action_types=frozenset({"push_notification"}),
    )):
        out = asyncio.run(propose_action({
            "type": "push_notification",
            "payload": {"title": "Test", "message": "Body", "agent": "spoofed"},
            "rationale": "council judged this URGENT and not duplicate of pending",
            "source_finding_ids": ["f-xyz"],
            "risk": "confirm",
        }))
    text = out["content"][0]["text"]
    action_id = text.split()[1]
    row = queue.get(action_id)
    queue.record_review_delivery(
        action_id, row["payload_hash"], "council", "CCOUNCIL", "1.1"
    )

    assert approvals.handle_reply(
        f"yes {action_id}", area="council", actor="UOWNER", owner=True
    ) is True
    assert queue.get(action_id)["status"] == "approved"

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(return_value={"status": "noop"})
        ninst.persona_for = lambda *a, **k: None
        nfac.return_value = ninst

        asyncio.run(executor.drain_once())
        ninst.send.assert_awaited_once()

    row = queue.get(action_id)
    assert row["status"] == "executed"
    assert row["approval_channel"] == "slack_reply"
    assert "yes " in row["approval_signal"]
