from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.brain.actions.handlers.send_draft import execute as execute_send_draft


def _approved_draft_payload(vault, draft):
    from src.brain.actions.handlers._draft_io import snapshot_draft

    relative, snapshot = snapshot_draft(vault, draft.relative_to(vault))
    return {"draft_path": relative, **snapshot}


def test_every_action_type_has_a_handler() -> None:
    from src.brain.actions.handlers import HANDLERS
    from src.brain.actions.types import ActionType

    assert set(HANDLERS) == set(ActionType)


@pytest.mark.parametrize("action_type", ["edit_vault_files", "promote_note", "promote_intake"])
def test_confirm_only_types_default_to_confirm_in_the_risk_policy(action_type) -> None:
    from src.brain.mcp.tools.actions import _risk_policy

    _risk_policy.cache_clear()
    try:
        assert _risk_policy().get(action_type) == "confirm"
    finally:
        _risk_policy.cache_clear()


def test_push_notification_uses_stable_per_action_delivery_keys(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    async def run_once():
        with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
            ninst = AsyncMock()
            ninst.send = AsyncMock(side_effect=[
                {"status": "sent", "id": "111.222", "group_id": "C_AREA"},
                {"status": "sent", "id": "111.333", "group_id": "C_AREA"},
            ])
            ninst.persona_for = lambda *a, **k: {"name": "Agent"}
            nfac.return_value = ninst
            result = await execute({
                "message": "summary",
                "thread_detail": "detail",
                "agent": "career",
                "area": "career",
                "_action_id": "abc123",
            })
            return result, [call.kwargs["idempotency_key"] for call in ninst.send.await_args_list]

    first, first_keys = asyncio.run(run_once())
    second, second_keys = asyncio.run(run_once())

    assert first["ok"] is True and second["ok"] is True
    assert first_keys == second_keys
    assert first_keys[0] != first_keys[1]


def test_push_notification_handler_posts_detail_in_thread(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(side_effect=[
            {"status": "sent", "id": "111.222", "group_id": "C_AREA"},
            {"status": "sent", "id": "111.333", "group_id": "C_AREA"},
        ])
        ninst.persona_for = lambda *a, **k: {"name": "Agent"}
        nfac.return_value = ninst

        result = asyncio.run(execute({
            "title": "One topic",
            "message": "Short summary",
            "thread_detail": "Evidence and next steps",
            "agent": "career",
            "area": "career",
        }))

    assert result["ok"] is True
    assert ninst.send.await_count == 2
    root_call, detail_call = ninst.send.await_args_list
    assert root_call.kwargs.get("quote_timestamp") is None
    assert root_call.kwargs["require_explicit_area"] is True
    assert detail_call.args[0] == "Evidence and next steps"
    assert detail_call.kwargs["quote_timestamp"] == "111.222"
    assert detail_call.kwargs["require_explicit_area"] is True


def test_push_notification_records_partial_thread_failure(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(side_effect=[
            {"status": "sent", "id": "111.222", "group_id": "C_AREA"},
            {"status": "failed", "error": "thread rejected", "group_id": "C_AREA"},
        ])
        ninst.persona_for = lambda *a, **k: {"name": "Agent"}
        nfac.return_value = ninst

        result = asyncio.run(execute({
            "title": "One topic",
            "message": "Short summary",
            "thread_detail": "Evidence and next steps",
            "agent": "career",
            "area": "career",
        }))

    assert result["ok"] is True
    assert result["partial"] is True
    assert "detail not delivered" in result["result"]
    statuses = [
        row[0]
        for row in isolated_db.get_db().execute(
            "SELECT status FROM notifications ORDER BY created_at, rowid"
        ).fetchall()
    ]
    assert statuses == ["sent", "failed"]


def test_push_notification_stamps_only_after_complete_verified_reminder_delivery(
    isolated_db,
) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with (
        patch("src.brain.actions.handlers.push_notification.notifier") as nfac,
        patch(
            "src.brain.occasions.store.reminder_identity_status", return_value="due"
        ) as status,
        patch("src.brain.occasions.store.stamp_reminded", return_value=True) as stamp,
    ):
        ninst = AsyncMock()
        ninst.send = AsyncMock(side_effect=[
            {"status": "sent", "id": "111.222", "group_id": "C_HUMANS"},
            {"status": "sent", "id": "111.333", "group_id": "C_HUMANS"},
        ])
        ninst.persona_for = lambda *a, **k: {"name": "Humans"}
        nfac.return_value = ninst
        result = asyncio.run(execute({
            "title": "Ravi's birthday",
            "message": "Tomorrow",
            "thread_detail": "Suggested wording",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": 2026,
            "_action_id": "abc123def456",
            "_origin_agent": "humans",
            "_origin_area": "humans",
            "_action_created_at": "2026-08-25T10:00:00+00:00",
        }))

    assert result["ok"] is True
    status.assert_called_once()
    stamp.assert_called_once_with("ravi", "birthday", 2026, ts="111.222")


@pytest.mark.parametrize(
    ("responses", "thread_detail"),
    [
        ([{"status": "noop"}], ""),
        ([{"status": "sent", "id": ""}], ""),
        ([
            {"status": "sent", "id": "111.222"},
            {"status": "noop"},
        ], "Suggested wording"),
        ([
            {"status": "sent", "id": "111.222"},
            {"status": "sent", "id": ""},
        ], "Suggested wording"),
        ([
            {"status": "sent", "id": "111.222"},
            {"status": "failed", "error": "thread rejected"},
        ], "Suggested wording"),
    ],
)
def test_incomplete_reminder_delivery_never_stamps(
    isolated_db, responses, thread_detail
) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with (
        patch("src.brain.actions.handlers.push_notification.notifier") as nfac,
        patch(
            "src.brain.occasions.store.reminder_identity_status", return_value="due"
        ),
        patch("src.brain.occasions.store.stamp_reminded") as stamp,
    ):
        ninst = AsyncMock()
        ninst.send = AsyncMock(side_effect=responses)
        ninst.persona_for = lambda *a, **k: {"name": "Humans"}
        nfac.return_value = ninst
        result = asyncio.run(execute({
            "title": "Ravi's birthday",
            "message": "Tomorrow",
            "thread_detail": thread_detail,
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": 2026,
            "_action_id": "abc123def456",
            "_origin_agent": "humans",
            "_origin_area": "humans",
            "_action_created_at": "2026-08-25T10:00:00+00:00",
        }))

    assert result["ok"] is False or result.get("partial") is True
    stamp.assert_not_called()


@pytest.mark.parametrize(
    ("reminder_status", "expected_ok"),
    [("already_reminded", True), ("stale", False), ("invalid", False)],
)
def test_duplicate_or_stale_reminder_is_not_sent(
    isolated_db, reminder_status, expected_ok
) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with (
        patch("src.brain.actions.handlers.push_notification.notifier") as nfac,
        patch(
            "src.brain.occasions.store.reminder_identity_status",
            return_value=reminder_status,
        ),
        patch("src.brain.occasions.store.stamp_reminded") as stamp,
    ):
        result = asyncio.run(execute({
            "title": "Ravi's birthday",
            "message": "Tomorrow",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": 2026,
            "_action_id": "abc123def456",
            "_origin_agent": "humans",
            "_origin_area": "humans",
            "_action_created_at": "2026-08-25T10:00:00+00:00",
        }))

    assert result["ok"] is expected_ok
    nfac.assert_not_called()
    stamp.assert_not_called()


def test_occasion_prose_without_explicit_marker_never_stamps(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with (
        patch("src.brain.actions.handlers.push_notification.notifier") as nfac,
        patch("src.brain.occasions.store.stamp_reminded") as stamp,
    ):
        ninst = AsyncMock()
        ninst.send = AsyncMock(return_value={"status": "sent", "id": "111.222"})
        ninst.persona_for = lambda *a, **k: {"name": "Humans"}
        nfac.return_value = ninst
        result = asyncio.run(execute({
            "message": "Ravi's birthday is tomorrow",
            "_origin_agent": "humans",
            "_origin_area": "humans",
        }))

    assert result["ok"] is True
    stamp.assert_not_called()


def test_invalid_occasion_marker_fails_before_delivery(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        result = asyncio.run(execute({
            "message": "Tomorrow",
            "occasion_person_id": "ravi",
            "_origin_agent": "humans",
            "_origin_area": "humans",
        }))

    assert result["ok"] is False
    nfac.assert_not_called()


def test_push_notification_propagates_retryable_delivery_failure(isolated_db) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(return_value={
            "status": "failed",
            "error": "temporary transport error",
            "retryable": True,
        })
        ninst.persona_for = lambda *a, **k: None
        nfac.return_value = ninst

        result = asyncio.run(execute({
            "message": "World",
            "agent": "test",
            "_action_id": "action-1",
        }))

    assert result["ok"] is False
    assert result["retryable"] is True


def test_push_notification_marks_unkeyed_uncertain_delivery_ambiguous(
    isolated_db,
) -> None:
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(return_value={
            "status": "failed",
            "error": "internal_error",
            "ambiguous": True,
        })
        ninst.persona_for = lambda *a, **k: None
        nfac.return_value = ninst

        result = asyncio.run(execute({"message": "World", "agent": "test"}))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True


def test_send_draft_marks_file_sent_and_copies_to_sent_dir(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-04-18-reply-x.md"
    draft.write_text(
        "---\nkind: reply\nstatus: pending\n---\n\n# X\n\nbody here\n",
        encoding="utf-8",
    )

    from src.brain.actions.handlers.send_draft import execute

    result = asyncio.run(execute(_approved_draft_payload(isolated_vault, draft)))
    assert result["ok"] is True

    sent = isolated_vault / "00 - Inbox" / "sent" / "2026-04-18-reply-x.md"
    assert sent.exists()
    assert "status: sent" in draft.read_text(encoding="utf-8")


def test_send_draft_message_kind_calls_notifier(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-04-18-message-x.md"
    draft.write_text(
        "---\nkind: message\nstatus: pending\nrecipient: career\n---\n\n# X\n\nHey there\n",
        encoding="utf-8",
    )

    fake_notifier = AsyncMock()
    fake_notifier.send = AsyncMock(return_value={"status": "sent", "id": "1712345678.000123"})
    with patch(
        "src.brain.mcp._common.notifier",
        return_value=fake_notifier,
    ):
        result = asyncio.run(
            execute_send_draft(_approved_draft_payload(isolated_vault, draft))
        )

    assert result["ok"] is True
    assert "sent" in result["result"].lower()
    fake_notifier.send.assert_awaited_once()
    _, kwargs = fake_notifier.send.call_args
    assert kwargs.get("area") == "career"
    assert "Hey there" in fake_notifier.send.call_args.args[0]
    assert "status: sent" in draft.read_text(encoding="utf-8")
    assert (isolated_vault / "00 - Inbox" / "sent" / draft.name).exists()


def test_send_draft_message_kind_marks_failed_on_send_error(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-04-18-message-y.md"
    draft.write_text(
        "---\nkind: message\nstatus: pending\nrecipient: career\n---\n\nbody\n",
        encoding="utf-8",
    )

    fake_notifier = AsyncMock()
    fake_notifier.send = AsyncMock(return_value={"status": "failed", "error": "401 unauthorized"})
    with patch(
        "src.brain.mcp._common.notifier",
        return_value=fake_notifier,
    ):
        result = asyncio.run(
            execute_send_draft(_approved_draft_payload(isolated_vault, draft))
        )

    assert result["ok"] is False
    assert "401" in result["result"] or "failed" in result["result"].lower()
    assert "status: pending" in draft.read_text(encoding="utf-8")


def test_send_draft_does_not_retry_after_delivery_if_archive_fails(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-04-18-message-partial.md"
    draft.write_text(
        "---\nkind: message\nstatus: pending\nrecipient: career\n---\n\nbody\n",
        encoding="utf-8",
    )

    fake_notifier = AsyncMock()
    fake_notifier.send = AsyncMock(return_value={"status": "sent", "id": "171.1"})
    with patch("src.brain.mcp._common.notifier", return_value=fake_notifier), patch(
        "src.brain.actions.handlers.send_draft.mark_sent_and_copy",
        side_effect=OSError("disk full"),
    ):
        result = asyncio.run(
            execute_send_draft(_approved_draft_payload(isolated_vault, draft))
        )

    assert result["ok"] is True
    assert result["partial"] is True
    assert "bookkeeping failed" in result["result"]
    fake_notifier.send.assert_awaited_once()


def test_send_draft_unknown_kind_falls_back_to_file_only(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-04-18-doc-z.md"
    draft.write_text(
        "---\nkind: doc\nstatus: pending\n---\n\n# title\n\nbody\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        execute_send_draft(_approved_draft_payload(isolated_vault, draft))
    )
    assert result["ok"] is True
    assert "status: sent" in draft.read_text(encoding="utf-8")
    assert (isolated_vault / "00 - Inbox" / "sent" / draft.name).exists()


def test_send_draft_rejects_content_changed_after_approval(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-08-23-message-approved.md"
    draft.write_text(
        "---\nkind: message\ntitle: Approved\nstatus: pending\n"
        "recipient: career\n---\n\nApproved body\n",
        encoding="utf-8",
    )
    payload = _approved_draft_payload(isolated_vault, draft)
    draft.write_text(
        "---\nkind: message\ntitle: Approved\nstatus: pending\n"
        "recipient: career\n---\n\nChanged after approval\n",
        encoding="utf-8",
    )
    fake_notifier = AsyncMock()
    fake_notifier.send = AsyncMock(return_value={"status": "sent", "id": "should-not-send"})

    with patch("src.brain.mcp._common.notifier", return_value=fake_notifier):
        result = asyncio.run(execute_send_draft(payload))

    assert result["ok"] is False
    assert "changed after approval" in result["result"]
    fake_notifier.send.assert_not_awaited()


def test_send_draft_bookkeeping_does_not_overwrite_a_concurrent_edit(
    isolated_vault, monkeypatch
) -> None:
    from src.brain.actions.handlers import _draft_io
    from src.brain.atomic_io import atomic_write_text_if_sha256

    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-08-23-doc-race.md"
    draft.write_text(
        "---\nkind: doc\nstatus: pending\n---\n\nApproved body\n",
        encoding="utf-8",
    )
    payload = _approved_draft_payload(isolated_vault, draft)

    def race(path, text, expected_sha256):
        path.write_text("human edit during bookkeeping\n", encoding="utf-8")
        atomic_write_text_if_sha256(path, text, expected_sha256)

    monkeypatch.setattr(_draft_io, "atomic_write_text_if_sha256", race)
    result = asyncio.run(execute_send_draft(payload))

    assert result["ok"] is False
    assert "changed before bookkeeping" in result["result"]
    assert draft.read_text(encoding="utf-8") == "human edit during bookkeeping\n"


def test_send_draft_retry_recognizes_its_approved_snapshot(isolated_vault) -> None:
    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-08-23-doc-idempotent.md"
    draft.write_text(
        "---\nkind: doc\ntitle: Stable\nstatus: pending\n---\n\nApproved body\n",
        encoding="utf-8",
    )
    payload = _approved_draft_payload(isolated_vault, draft)

    first = asyncio.run(execute_send_draft(payload))
    second = asyncio.run(execute_send_draft(payload))

    assert first["ok"] is True
    assert second == {"ok": True, "result": "already sent (idempotent no-op)"}
    assert payload["draft_sha256"] in draft.read_text(encoding="utf-8")
