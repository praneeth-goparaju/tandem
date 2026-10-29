from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def test_trusted_client_channel_cannot_authorize_other_effects(isolated_db) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.types import (
        TRUSTED_CLIENT_APPROVAL_CHANNEL,
        Action,
        ActionType,
        Risk,
    )

    action = Action(
        id="d1",
        type=ActionType.SEND_DRAFT,
        payload={"draft_path": "Draft.md"},
        rationale="must stay human-gated",
        risk=Risk.AUTO,
        origin_agent="external-mcp",
    )
    queue.insert(action)
    queue.transition_status(
        "d1",
        "pending",
        "approved",
        channel=TRUSTED_CLIENT_APPROVAL_CHANNEL,
        actor="external-mcp",
    )
    fake_handler = AsyncMock(return_value={"ok": True, "result": "sent"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.SEND_DRAFT: fake_handler},
        clear=False,
    ):
        asyncio.run(executor.drain_once())

    assert queue.get("d1")["status"] == "failed"
    fake_handler.assert_not_awaited()
