from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

_MAIL_SNAPSHOT = {
    "uidvalidity": "7",
    "message_id": "<m@example.com>",
    "message_from": "Sender <sender@example.com>",
    "message_subject": "Subject",
    "message_header_sha256": "a" * 64,
}


def _call(tool, *, caller=None, **kwargs):
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    caller = caller or ToolCaller(
        agent="test", area="test",
        allowed_action_types=frozenset(item.value for item in ActionType),
    )
    handler = getattr(tool, "handler", tool)
    with caller_scope(caller):
        return asyncio.run(handler(kwargs))


def test_auto_risk_runs_handler_synchronously_and_marks_executed(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    fake_handler = AsyncMock(return_value={"ok": True, "result": "notifier:noop"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: fake_handler},
        clear=False,
    ):
        out = _call(
            propose_action,
            type="push_notification",
            payload={"title": "T", "message": "M"},
            rationale="auto-approved sync path test",
            risk="auto",
        )

    text = out["content"][0]["text"]
    assert "executed" in text.lower()
    assert "notifier:noop" in text

    rows = queue.list_by_status("executed")
    assert len(rows) == 1
    assert rows[0]["approval_channel"] == "auto"
    fake_handler.assert_awaited_once()


def test_trusted_client_mail_archive_executes_without_slack(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import (
        TRUSTED_CLIENT_APPROVAL_CHANNEL,
        ActionType,
    )
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.mail_tools import mail_archive

    async def snapshot(provider, uid, mailbox):
        assert (provider, uid, mailbox) == ("icloud", "42", "INBOX")
        return _MAIL_SNAPSHOT

    monkeypatch.setattr(mail_ops, "snapshot_message", snapshot)
    caller = ToolCaller(
        agent="external-mcp",
        area="interactive-mcp",
        allowed_action_types=frozenset({ActionType.MAIL_ARCHIVE.value}),
        direct_action_types=frozenset({ActionType.MAIL_ARCHIVE.value}),
    )
    handler = AsyncMock(return_value={"ok": True, "result": "archived"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.MAIL_ARCHIVE: handler},
        clear=False,
    ):
        out = _call(
            mail_archive,
            caller=caller,
            provider="icloud",
            uid="42",
            rationale="user selected this message",
            risk="confirm",
        )

    assert "executed" in out["content"][0]["text"]
    row = queue.list_by_status("executed")[0]
    assert row["risk"] == "auto"
    assert row["approval_channel"] == TRUSTED_CLIENT_APPROVAL_CHANNEL
    assert row["approval_actor"] == "external-mcp"
    handler.assert_awaited_once()


def test_trusted_client_completes_reminder_without_slack(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import TRUSTED_CLIENT_APPROVAL_CHANNEL, ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.icloud_actions import complete_reminder

    caller = ToolCaller(
        agent="external-mcp",
        area="interactive-mcp",
        allowed_action_types=frozenset({ActionType.COMPLETE_REMINDER.value}),
        direct_action_types=frozenset({ActionType.COMPLETE_REMINDER.value}),
    )
    handler = AsyncMock(return_value={"ok": True, "result": "completed reminder"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.COMPLETE_REMINDER: handler},
        clear=False,
    ):
        out = _call(
            complete_reminder,
            caller=caller,
            id="reminder-42",
            rationale="user confirmed completion in trusted local chat",
        )

    assert "executed" in out["content"][0]["text"]
    row = queue.list_by_status("executed")[0]
    assert row["approval_channel"] == TRUSTED_CLIENT_APPROVAL_CHANNEL
    assert row["approval_actor"] == "external-mcp"
    handler.assert_awaited_once()
    assert handler.await_args.args[0]["id"] == "reminder-42"


def test_a_tainted_caller_completes_a_reminder_without_approval(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.icloud_actions import complete_reminder

    async def _fake_run_one(row):
        queue.transition_status(row["id"], "approved", "executed", result="ok")

    monkeypatch.setattr("src.brain.actions.executor._run_one", _fake_run_one)
    caller = ToolCaller(
        agent="scheduled-agent",
        area="family",
        allowed_action_types=frozenset({ActionType.COMPLETE_REMINDER.value}),
        direct_action_types=frozenset({ActionType.COMPLETE_REMINDER.value}),
        untrusted_input=True,
    )
    out = _call(
        complete_reminder,
        caller=caller,
        id="reminder-42",
        rationale="untrusted request",
    )

    assert "risk=confirm" not in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []
    assert queue.list_by_status("executed")[0]["risk"] == "auto"


def test_tainted_caller_cannot_use_trusted_client_archive_grant(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.mail_tools import mail_archive

    async def snapshot(_provider, _uid, _mailbox):
        return _MAIL_SNAPSHOT

    monkeypatch.setattr(mail_ops, "snapshot_message", snapshot)
    caller = ToolCaller(
        agent="scheduled-agent",
        area="inbox",
        allowed_action_types=frozenset({ActionType.MAIL_ARCHIVE.value}),
        direct_action_types=frozenset({ActionType.MAIL_ARCHIVE.value}),
        is_reply=True,
        untrusted_input=True,
    )
    out = _call(
        mail_archive,
        caller=caller,
        provider="icloud",
        uid="42",
        rationale="mail-derived request",
        risk="auto",
    )

    assert "risk=confirm" in out["content"][0]["text"]
    assert len(queue.list_by_status("pending")) == 1


def test_trusted_client_retry_executes_matching_pending_archive(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.mail_tools import mail_archive

    async def snapshot(_provider, _uid, _mailbox):
        return _MAIL_SNAPSHOT

    monkeypatch.setattr(mail_ops, "snapshot_message", snapshot)
    base = {
        "agent": "external-mcp",
        "area": "interactive-mcp",
        "allowed_action_types": frozenset({ActionType.MAIL_ARCHIVE.value}),
    }
    pending_caller = ToolCaller(**base)
    direct_caller = ToolCaller(
        **base,
        direct_action_types=frozenset({ActionType.MAIL_ARCHIVE.value}),
    )
    args = {
        "provider": "icloud",
        "uid": "42",
        "rationale": "same selected message",
    }
    first = _call(mail_archive, caller=pending_caller, **args)
    assert "queued" in first["content"][0]["text"]

    handler = AsyncMock(return_value={"ok": True, "result": "archived once"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.MAIL_ARCHIVE: handler},
        clear=False,
    ):
        second = _call(mail_archive, caller=direct_caller, **args)

    assert "executed" in second["content"][0]["text"]
    assert len(queue.list_by_status("executed")) == 1
    assert queue.list_by_status("pending") == []
    handler.assert_awaited_once()


def test_tainted_proactive_push_notify_executes_fixed_route_notification(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.notifications import push_notify

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    handler = AsyncMock(return_value={"ok": True, "result": "sent"})
    with (
        patch.dict(
            "src.brain.actions.handlers.HANDLERS",
            {ActionType.PUSH_NOTIFICATION: handler},
            clear=False,
        ),
        patch("src.brain.mcp.tools.actions.notifier") as review_notifier,
    ):
        out = _call(
            push_notify,
            caller=caller,
            title="Status",
            message="Read-derived report",
        )

    assert "executed" in out["content"][0]["text"]
    assert len(queue.list_by_status("executed")) == 1
    assert queue.list_by_status("pending") == []
    handler.assert_awaited_once()
    review_notifier.assert_not_called()


def test_tainted_push_notify_with_approval_ids_posts_without_double_confirmation(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.notifications import push_notify

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    queue.insert(Action(
        id="abcdef123456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Call Ravi"},
        rationale="Ask the owner first.",
        origin_agent="humans",
        origin_area="humans",
    ))
    handler = AsyncMock(return_value={"ok": True, "result": "review posted"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: handler},
        clear=False,
    ):
        out = _call(
            push_notify,
            caller=caller,
            title="Status",
            message="Framed approval",
            proposal_action_ids=["abcdef123456"],
        )

    assert "executed" in out["content"][0]["text"]
    assert [row["id"] for row in queue.list_by_status("pending")] == [
        "abcdef123456"
    ]
    handler.assert_awaited_once()
    payload = handler.await_args.args[0]
    assert payload["proposal_action_ids"] == ["abcdef123456"]


def test_model_input_cannot_forge_fixed_route_notification_provenance(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.actions import propose_action

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    review_notifier = AsyncMock()
    review_notifier.send = AsyncMock(return_value={
        "status": "failed", "error": "test",
    })
    with patch(
        "src.brain.mcp.tools.actions.notifier", return_value=review_notifier
    ):
        out = _call(
            propose_action,
            caller=caller,
            _fixed_route_notification=True,
            type="push_notification",
            payload={"message": "raw proposal"},
            rationale="r",
            risk="auto",
        )

    assert "risk=confirm" in out["content"][0]["text"]
    assert len(queue.list_by_status("pending")) == 1
    review_notifier.send.assert_awaited_once()


def test_auto_risk_returns_handler_failure_result(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    failing = AsyncMock(return_value={"ok": False, "result": "downstream API rejected"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: failing},
        clear=False,
    ):
        out = _call(
            propose_action,
            type="push_notification",
            payload={"message": "valid payload"},
            rationale="r",
            risk="auto",
        )

    text = out["content"][0]["text"]
    assert "FAILED" in text or "failed" in text
    assert "downstream API rejected" in text
    rows = queue.list_by_status("failed")
    assert len(rows) == 1


def test_invalid_payload_never_reaches_queue_or_auto_handler(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    handler = AsyncMock(return_value={"ok": True, "result": "should not run"})
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: handler},
        clear=False,
    ):
        for risk in ("auto", "confirm"):
            out = _call(
                propose_action,
                type="push_notification",
                payload={},
                rationale="invalid payload must fail closed",
                risk=risk,
            )
            assert "requires non-empty message" in out["content"][0]["text"]

    assert queue.list_by_status("pending") == []
    assert queue.list_by_status("executed") == []
    handler.assert_not_awaited()


def test_lost_auto_response_retry_uses_executed_receipt(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    handler = AsyncMock(return_value={"ok": True, "result": "sent once"})
    kwargs = {
        "type": "push_notification",
        "payload": {"title": "Stable", "message": "One occurrence"},
        "rationale": "response-loss retry",
        "risk": "auto",
    }
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: handler},
        clear=False,
    ):
        first = _call(propose_action, **kwargs)
        action_id = queue.list_by_status("executed")[0]["id"]
        second = _call(propose_action, **kwargs)

    assert f"action {action_id} executed" in first["content"][0]["text"]
    assert (
        f"action {action_id} already executed" in second["content"][0]["text"]
    )
    assert "pending" not in second["content"][0]["text"]
    handler.assert_awaited_once()


def test_distinct_occurrence_keys_allow_legitimate_auto_repeats(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    handler = AsyncMock(return_value={"ok": True, "result": "sent"})
    shared = {
        "type": "push_notification",
        "payload": {"title": "Daily", "message": "Same body"},
        "rationale": "intentional daily occurrence",
        "risk": "auto",
    }
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: handler},
        clear=False,
    ):
        _call(propose_action, **shared, occurrence_key="daily:2026-08-23")
        _call(propose_action, **shared, occurrence_key="daily:2026-08-24")

    assert len(queue.list_by_status("executed")) == 2
    assert handler.await_count == 2


def test_auto_risk_reports_temporary_failure_as_queued_retry(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    temporary = AsyncMock(return_value={
        "ok": False,
        "result": "service unavailable",
        "retryable": True,
    })
    with patch.dict(
        "src.brain.actions.handlers.HANDLERS",
        {ActionType.PUSH_NOTIFICATION: temporary},
        clear=False,
    ):
        out = _call(
            propose_action,
            type="push_notification",
            payload={"message": "retry me"},
            rationale="r",
            risk="auto",
        )

    text = out["content"][0]["text"]
    assert "queued for retry" in text
    rows = queue.list_by_status("approved")
    assert len(rows) == 1
    assert rows[0]["retry_count"] == 1


def test_tainted_reply_leaves_review_delivery_to_reply_card(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.actions import propose_action

    caller = ToolCaller(
        agent="humans", area="humans", is_reply=True, untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    with patch("src.brain.mcp.tools.actions.notifier") as direct_notifier:
        out = _call(
            propose_action,
            caller=caller,
            type="push_notification",
            payload={"message": "Needs review"},
            rationale="r",
            risk="auto",
        )

    assert "queued" in out["content"][0]["text"]
    assert len(queue.list_by_status("pending")) == 1
    direct_notifier.assert_not_called()


def test_tainted_scheduled_review_retry_is_idempotent(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.actions import propose_action

    keys: list[str] = []

    async def deliver(_text, **kwargs):
        keys.append(kwargs["idempotency_key"])
        if len(keys) == 1:
            return {"status": "failed", "error": "temporary", "retryable": True}
        action_id = kwargs["proposal_action_ids"][0]
        row = queue.get(action_id)
        queue.record_review_delivery(
            action_id, row["payload_hash"], "humans", "C_HUMANS", "100.200"
        )
        return {"status": "sent", "id": "100.200"}

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    review_notifier = AsyncMock()
    review_notifier.send = AsyncMock(side_effect=deliver)
    kwargs = {
        "type": "push_notification",
        "payload": {"message": "Needs review"},
        "rationale": "r",
        "risk": "auto",
    }
    with patch(
        "src.brain.mcp.tools.actions.notifier", return_value=review_notifier
    ):
        first = _call(propose_action, caller=caller, **kwargs)
        second = _call(propose_action, caller=caller, **kwargs)

    assert "approval review delivered" in first["content"][0]["text"]
    assert "approval review delivered" in second["content"][0]["text"]
    assert keys[0] == keys[1]
    assert review_notifier.send.await_count == 2
    assert len(queue.list_by_status("pending")) == 1


def test_tainted_scheduled_review_does_not_retry_permanent_failure(isolated_db) -> None:
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.actions import propose_action

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    review_notifier = AsyncMock()
    review_notifier.send = AsyncMock(return_value={
        "status": "failed", "error": "invalid_auth",
    })
    with patch(
        "src.brain.mcp.tools.actions.notifier", return_value=review_notifier
    ):
        out = _call(
            propose_action,
            caller=caller,
            type="push_notification",
            payload={"message": "Needs review"},
            rationale="r",
            risk="auto",
        )

    assert "could not be delivered" in out["content"][0]["text"]
    review_notifier.send.assert_awaited_once()


def test_tainted_scheduled_review_retries_missing_receipt(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller
    from src.brain.mcp.tools.actions import propose_action

    keys: list[str] = []

    async def deliver(_text, **kwargs):
        keys.append(kwargs["idempotency_key"])
        if len(keys) == 2:
            action_id = kwargs["proposal_action_ids"][0]
            row = queue.get(action_id)
            queue.record_review_delivery(
                action_id, row["payload_hash"], "humans", "C_HUMANS", "100.200"
            )
        return {"status": "sent", "id": "100.200"}

    caller = ToolCaller(
        agent="humans", area="humans", untrusted_input=True,
        allowed_action_types=frozenset({ActionType.PUSH_NOTIFICATION.value}),
    )
    review_notifier = AsyncMock()
    review_notifier.send = AsyncMock(side_effect=deliver)
    with patch(
        "src.brain.mcp.tools.actions.notifier", return_value=review_notifier
    ):
        out = _call(
            propose_action,
            caller=caller,
            type="push_notification",
            payload={"message": "Needs review"},
            rationale="r",
            risk="auto",
        )

    assert "approval review delivered" in out["content"][0]["text"]
    assert keys[0] == keys[1]
    assert review_notifier.send.await_count == 2
