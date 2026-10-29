from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.brain.actions.handlers._draft_io import frontmatter_field
from src.brain.actions.handlers.send_draft import execute
from src.brain.plugins.base import DELIVERY_SUCCESS_STATES


def _message_payload(vault, draft, *, action_id: str | None) -> dict:
    from src.brain.actions.handlers._draft_io import snapshot_draft

    relative, snapshot = snapshot_draft(vault, draft.relative_to(vault))
    payload = {"draft_path": relative, **snapshot}
    if action_id is not None:
        payload["_action_id"] = action_id
    return payload


def _message_draft(vault):
    drafts = vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    draft = drafts / "message.md"
    draft.write_text(
        "---\nkind: message\nstatus: pending\narea: career\n---\n\nbody\n",
        encoding="utf-8",
    )
    return draft


def test_status_check_ignores_body_substring() -> None:
    text = (
        "---\n"
        "kind: doc\n"
        "recipient: someone@example.com\n"
        "---\n"
        "\n"
        "Hey, just confirming the status: sent for the report from yesterday.\n"
    )
    assert frontmatter_field(text, "status") is None


def test_status_sent_in_frontmatter_recognized() -> None:
    text = (
        "---\n"
        "kind: doc\n"
        "status: sent\n"
        "---\n"
        "\n"
        "body\n"
    )
    assert frontmatter_field(text, "status") == "sent"


@pytest.mark.parametrize("status", ["sent", "noop"])
def test_a_sent_or_noop_delivery_is_a_success(isolated_vault, status) -> None:
    draft = _message_draft(isolated_vault)
    fake = AsyncMock()
    fake.send = AsyncMock(return_value={"status": status, "id": "1.2"})
    with patch("src.brain.mcp._common.notifier", return_value=fake):
        result = asyncio.run(execute(_message_payload(isolated_vault, draft, action_id=f"a-{status}")))

    assert result["ok"] is True


def test_send_draft_uses_stable_action_delivery_key(isolated_vault) -> None:
    async def send_once():
        draft = _message_draft(isolated_vault)
        fake = AsyncMock()
        fake.send = AsyncMock(return_value={"status": "sent", "id": "1.2"})
        with patch("src.brain.mcp._common.notifier", return_value=fake):
            result = await execute(
                _message_payload(isolated_vault, draft, action_id="action-123")
            )
        return result, fake.send.await_args.kwargs["idempotency_key"]

    first, first_key = asyncio.run(send_once())
    second, second_key = asyncio.run(send_once())

    assert first["ok"] is True and second["ok"] is True
    assert first_key == second_key
    assert first_key


def test_send_draft_propagates_ambiguous_notifier_result(isolated_vault) -> None:
    draft = _message_draft(isolated_vault)
    fake = AsyncMock()
    fake.send = AsyncMock(
        return_value={"status": "failed", "error": "unknown", "ambiguous": True}
    )
    with patch("src.brain.mcp._common.notifier", return_value=fake):
        result = asyncio.run(
            execute(_message_payload(isolated_vault, draft, action_id="action-amb"))
        )

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True


def test_send_draft_unkeyed_timeout_is_ambiguous(isolated_vault) -> None:
    draft = _message_draft(isolated_vault)
    fake = AsyncMock()
    fake.send = AsyncMock(side_effect=TimeoutError("transport timeout"))
    with patch("src.brain.mcp._common.notifier", return_value=fake):
        result = asyncio.run(
            execute(_message_payload(isolated_vault, draft, action_id=None))
        )

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True
