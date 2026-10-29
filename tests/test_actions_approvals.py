from __future__ import annotations

import hashlib

import pytest


def test_parse_yes_no_extracts_decision_and_id() -> None:
    from src.brain.actions.approvals import parse

    assert parse("yes a1b2c3d4e5f6") == ("approved", "a1b2c3d4e5f6")
    assert parse("YES a1b2c3d4e5f6 ") == ("approved", "a1b2c3d4e5f6")
    assert parse("no  123abc456def") == ("rejected", "123abc456def")
    assert parse("hello world") is None
    assert parse("yes") is None


def test_parse_accepts_only_complete_explicit_command_forms() -> None:
    from src.brain.actions.approvals import parse

    assert parse("approved fe391267ae0f") == ("approved", "fe391267ae0f")
    assert parse("approve `fe391267ae0f`") == ("approved", "fe391267ae0f")
    assert parse("`cc3d344a624f`:white_check_mark:") == ("approved", "cc3d344a624f")
    assert parse("cc3d344a624f✅") == ("approved", "cc3d344a624f")
    assert parse("reject a457d4eedf9e") == ("rejected", "a457d4eedf9e")
    assert parse("`a457d4eedf9e` ❌") == ("rejected", "a457d4eedf9e")


def test_parse_refuses_questions_negations_and_surrounding_prose() -> None:
    from src.brain.actions.approvals import parse

    assert parse("✅") is None
    assert parse("approve abc123def456 and fed654cba321") is None
    assert parse("not approved abc123def456") is None
    assert parse("I don't approve abc123def456") is None
    assert parse("should I approve abc123def456?") is None
    assert parse("why was abc123def456 approved?") is None
    assert parse("reject abc123def456 please") is None
    assert parse("approved - create it abc123def456") is None
    assert parse("snooze abc123def456") is None


def test_handle_reply_transitions_action(isolated_db) -> None:
    from src.brain.actions import approvals, queue
    from src.brain.actions.types import Action, ActionType

    action_id = "abcdef123456"
    queue.insert(Action(
        id=action_id,
        type=ActionType.PUSH_NOTIFICATION,
        payload={"title": "Test", "message": "Body"},
        rationale="r",
    ))

    handled = approvals.handle_reply(
        f"yes {action_id}", area=None, actor="UOWNER", owner=True
    )
    assert handled is True
    assert queue.get(action_id)["status"] == "approved"
    assert queue.get(action_id)["approval_channel"] == "slack_reply"


def test_handle_reply_returns_false_for_non_match(isolated_db) -> None:
    from src.brain.actions.approvals import handle_reply

    assert handle_reply(
        "just chatting", area=None, actor="UOWNER", owner=True
    ) is False


def test_text_approval_requires_exact_delivered_review_for_scoped_action(
    isolated_db,
) -> None:
    from src.brain.actions import approvals, queue
    from src.brain.actions.types import Action, ActionType

    action_id = "abcdef123456"
    queue.insert(Action(
        id=action_id,
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Review me", "due": "2026-09-01T09:00:00+02:00"},
        rationale="r",
        origin_area="family",
    ))
    assert approvals.handle_reply(
        f"approve {action_id}", area="family", actor="UOWNER", owner=True
    )
    row = queue.get(action_id)
    assert row and row["status"] == "pending"

    queue.record_review_delivery(
        action_id, row["payload_hash"], "family", "CFAMILY", "1.2"
    )
    assert approvals.handle_reply(
        f"approve {action_id}", area="family", actor="UOWNER", owner=True
    )
    assert queue.get(action_id)["status"] == "approved"


def test_review_rejects_payload_hash_mismatch() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.UPDATE_CONTACT,
        payload={"href": "contacts/1.vcf", "note": "expected"},
        rationale="r",
    ).to_row()
    row["payload_hash"] = "0" * 64

    review = build_approval_review(row)
    assert review.approvable is False
    assert review.reason == "queued payload failed its integrity check"


def test_review_rejects_legacy_vault_cascade_over_shared_limit() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import (
        MAX_VAULT_FILE_CHANGES,
        Action,
        ActionType,
    )

    row = Action(
        id="abcdef123456",
        type=ActionType.EDIT_VAULT_FILES,
        payload={
            "changes": [
                {"path": f"note-{index}.md", "new_content": "complete\n"}
                for index in range(MAX_VAULT_FILE_CHANGES + 1)
            ],
        },
        rationale="legacy oversized batch",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "8-file review limit" in review.reason


def test_review_disables_hidden_or_non_markdown_vault_targets() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    for path in (".obsidian/plugins/main.md", "Notes/config.json"):
        row = Action(
            id="abcdef123456",
            type=ActionType.EDIT_VAULT_FILE,
            payload={"path": path, "new_content": "complete\n"},
            rationale="legacy unsafe target",
        ).to_row()
        review = build_approval_review(row)
        assert review.approvable is False, path
        assert "unsafe path" in review.reason


def test_review_rejects_vault_diff_too_large_to_show(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.EDIT_VAULT_FILE,
        payload={
            "path": "Note.md", "new_content": "x" * 180_000,
            "expected_sha256": None,
        },
        rationale="large replacement",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "too large for Slack" in review.reason


def test_every_action_type_has_a_complete_review_surface(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.handlers._draft_io import snapshot_draft
    from src.brain.actions.handlers.promote_intake import intake_snapshot
    from src.brain.actions.types import Action, ActionType
    from src.memory import store

    draft = isolated_vault / "00 - Inbox" / "drafts" / "message.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("---\nkind: message\ntitle: Message\n---\n\nBody\n", encoding="utf-8")
    draft_path, draft_snapshot = snapshot_draft(isolated_vault, draft.relative_to(isolated_vault))
    intake_id = store.intake_add("test", "source-1", "capture", "Captured text")
    assert intake_id

    payloads = {
        ActionType.SEND_DRAFT: {
            "draft_path": draft_path,
            **draft_snapshot,
        },
        ActionType.SEND_BOT_MESSAGE: {
            "destination_area": "health",
            "title": "Update",
            "message": "Plain-text message",
        },
        ActionType.PUSH_NOTIFICATION: {"title": "Title", "message": "Body"},
        ActionType.CREATE_TODOIST_TASK: {
            "content": "Task",
            "project_id": "Inbox",
        },
        ActionType.EDIT_VAULT_FILE: {
            "path": "Note.md", "new_content": "Body\n", "expected_sha256": None,
        },
        ActionType.EDIT_VAULT_FILES: {
            "changes": [{
                "path": "Note.md", "new_content": "Body\n", "expected_sha256": None,
            }],
        },
        ActionType.CREATE_CALENDAR_EVENT: {
            "summary": "Event",
            "start": "2026-08-24T10:00:00+00:00",
            "end": "2026-08-24T11:00:00+00:00",
        },
        ActionType.UPDATE_CALENDAR_EVENT: {
            "event_url": "https://calendar.example/event.ics",
            "cancelled": True,
        },
        ActionType.MAIL_ARCHIVE: {
            "provider": "icloud", "mailbox": "INBOX", "uid": "42",
            "uidvalidity": "7", "message_id": "<m@example.com>",
            "message_from": "sender@example.com", "message_subject": "Subject",
            "message_header_sha256": "a" * 64,
        },
        ActionType.MAIL_ARCHIVE_BATCH: {"summary": "2 CI notices", "messages": [_ARCHIVED, {**_ARCHIVED, "uid": "43"}]},
        ActionType.CREATE_REMINDER: {
            "title": "Reminder", "due": "2026-09-01T09:00:00+02:00",
        },
        ActionType.COMPLETE_REMINDER: {"id": "reminder-42"},
        ActionType.CREATE_CONTACT: {"full_name": "Jane Doe"},
        ActionType.UPDATE_CONTACT: {
            "href": "contacts/42.vcf",
            "note": "Updated",
        },
        ActionType.PROMOTE_INTAKE: {
            "intake_id": intake_id,
            **intake_snapshot("Captured text", "test", "source-1"),
        },
        ActionType.PROMOTE_NOTE: {"body": "Knowledge\n"},
        ActionType.INSERT_VAULT_LINE: {
            "path": "Table.md", "anchor": "| last row |", "text": "| new row |",
        },
    }
    (isolated_vault / "Table.md").write_text("| last row |\n", encoding="utf-8")
    assert set(payloads) == set(ActionType)
    for action_type in ActionType:
        row = Action(
            id="abcdef123456",
            type=action_type,
            payload=payloads[action_type],
            rationale="r",
        ).to_row()
        assert build_approval_review(row).approvable, action_type


_ARCHIVED = {
    "provider": "icloud", "mailbox": "INBOX", "uid": "42",
    "uidvalidity": "7", "message_id": "<m@example.com>",
    "message_from": "GitHub <noreply@github.com>", "message_subject": "Run failed: CI",
    "message_header_sha256": "a" * 64,
}


def test_an_archive_batch_review_counts_the_mails_by_sender() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    messages = [{**_ARCHIVED, "uid": str(40 + n)} for n in range(3)]
    messages.append({**_ARCHIVED, "uid": "50", "message_from": "Crocs <news@shop.example.com>", "message_subject": "Sale"})
    row = Action(id="abcdef123456", type=ActionType.MAIL_ARCHIVE_BATCH,
                 payload={"summary": "CI notices and a promo", "messages": messages}, rationale="r").to_row()
    review = build_approval_review(row)
    assert review.approvable
    assert (review.title, review.note) == ("Archive 4 emails", "Moved out of the inbox, not deleted")
    assert review.summary_lines == ("• 3 from GitHub", "   Run failed: CI", "• 1 from Crocs", "   Sale")


@pytest.mark.parametrize("messages", [[], [{**_ARCHIVED, "uid": "042"}], [{**_ARCHIVED, "extra": "x"}], [_ARCHIVED] * 51])
def test_an_archive_batch_with_a_bad_message_cannot_be_approved(messages) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(id="abcdef123456", type=ActionType.MAIL_ARCHIVE_BATCH,
                 payload={"summary": "s", "messages": messages}, rationale="r").to_row()
    assert not build_approval_review(row).approvable


@pytest.mark.parametrize(
    ("field", "value"),
    [("uid", "042"), ("uidvalidity", "07"), ("message_header_sha256", "A" * 64)],
)
def test_mail_archive_review_rejects_noncanonical_identity(field, value) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    payload = {
        "provider": "icloud", "mailbox": "INBOX", "uid": "42",
        "uidvalidity": "7", "message_id": "<m@example.com>",
        "message_from": "sender@example.com", "message_subject": "Subject",
        "message_header_sha256": "a" * 64,
    }
    payload[field] = value
    row = Action(
        id="abcdef123456",
        type=ActionType.MAIL_ARCHIVE,
        payload=payload,
        rationale="archive the reviewed message",
    ).to_row()

    assert not build_approval_review(row).approvable


def test_mail_archive_card_shows_only_sender_and_subject() -> None:
    from src.brain.actions.approval_review import build_approval_review, summary_fields
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.MAIL_ARCHIVE,
        payload={
            "provider": "icloud", "mailbox": "INBOX", "uid": "42",
            "uidvalidity": "7", "message_id": "<m@example.com>",
            "message_from": "Run Team <news@example.com>",
            "message_subject": "Countdown & more",
            "message_header_sha256": "a" * 64,
        },
        rationale="newsletter, nothing to act on",
    ).to_row()

    review = build_approval_review(row)

    assert review.approvable
    assert review.summary_lines == (
        "From: Run Team",
        "Subject: Countdown ＆ more",
    )
    assert summary_fields(ActionType.MAIL_ARCHIVE) == (
        ("message_from", "From"), ("message_subject", "Subject"),
    )


def test_vault_review_shows_the_complete_effect_including_tail(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    target = isolated_vault / "Note.md"
    target.write_text("safe old\n", encoding="utf-8")
    row = Action(
        id="abcdef123456",
        type=ActionType.EDIT_VAULT_FILE,
        payload={
            "path": "Note.md",
            "new_content": "safe prefix\nmalicious hidden tail\n",
            "expected_sha256": hashlib.sha256(b"safe old\n").hexdigest(),
        },
        rationale="review the entire replacement",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is True
    rendered = "\n".join(review.summary_lines)
    assert "-safe old" in rendered
    assert "+safe prefix" in rendered
    assert "+malicious hidden tail" in rendered


def test_review_never_truncates_effect_fields(isolated_db) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    attendee = "final-attendee@example.test"
    calendar = Action(
        id="abcdef123456",
        type=ActionType.CREATE_CALENDAR_EVENT,
        payload={
            "summary": "Event",
            "start": "2026-08-24T10:00:00+00:00",
            "end": "2026-08-24T11:00:00+00:00",
            "attendees": [*(f"person-{i}@example.test" for i in range(30)), attendee],
        },
        rationale="r",
    ).to_row()
    note = Action(
        id="fed654cba321",
        type=ActionType.PROMOTE_NOTE,
        payload={"body": "safe prefix\n" + "x" * 500 + "\nHIDDEN_NOTE_TAIL"},
        rationale="r",
    ).to_row()

    assert attendee in "\n".join(build_approval_review(calendar).summary_lines)
    assert "HIDDEN_NOTE_TAIL" in "\n".join(build_approval_review(note).summary_lines)


def test_intake_review_shows_complete_bytes_and_rejects_snapshot_mismatch(
    isolated_db,
) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.handlers.promote_intake import intake_snapshot
    from src.brain.actions.types import Action, ActionType
    from src.memory import store

    content = "safe prefix\n" + "x" * 500 + "\nHIDDEN_INTAKE_TAIL"
    intake_id = store.intake_add("capture", "source-1", "note", content)
    assert intake_id
    payload = {
        "intake_id": intake_id,
        **intake_snapshot(content, "capture", "source-1"),
    }
    review = build_approval_review(Action(
        id="abcdef123456",
        type=ActionType.PROMOTE_INTAKE,
        payload=payload,
        rationale="r",
    ).to_row())
    assert review.approvable
    assert "HIDDEN_INTAKE_TAIL" in "\n".join(review.summary_lines)

    payload["intake_preview"] = "forged preview"
    mismatch = build_approval_review(Action(
        id="fed654cba321",
        type=ActionType.PROMOTE_INTAKE,
        payload=payload,
        rationale="r",
    ).to_row())
    assert not mismatch.approvable
    assert "changed after proposal" in mismatch.reason


def test_oversized_complete_effect_is_not_approvable() -> None:
    from src.brain.actions.approval_review import MAX_SUMMARY_CHARS, build_approval_review
    from src.brain.actions.types import Action, ActionType

    review = build_approval_review(Action(
        id="abcdef123456",
        type=ActionType.PROMOTE_NOTE,
        payload={"body": "x" * (MAX_SUMMARY_CHARS + 1)},
        rationale="r",
    ).to_row())
    assert not review.approvable
    assert "too large for Slack" in review.reason


def test_vault_review_disables_stale_action(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    (isolated_vault / "Note.md").write_text("changed later\n", encoding="utf-8")
    row = Action(
        id="abcdef123456",
        type=ActionType.EDIT_VAULT_FILE,
        payload={
            "path": "Note.md",
            "new_content": "replacement\n",
            "expected_sha256": hashlib.sha256(b"approved old\n").hexdigest(),
        },
        rationale="stale edit",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "changed after proposal" in review.reason


def test_review_rejects_unknown_top_level_effect_field() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.CREATE_REMINDER,
        payload={
            "title": "Review me",
            "due": "2026-09-01T09:00:00+02:00",
            "future_effect": "hidden",
        },
        rationale="r",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "unsupported fields: future_effect" in review.reason


def test_review_rejects_unknown_nested_vault_effect_field() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.EDIT_VAULT_FILES,
        payload={
            "changes": [
                {"path": "Note.md", "new_content": "Body\n", "append": True},
            ],
        },
        rationale="r",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "unsupported fields: append" in review.reason


def test_review_preserves_empty_false_and_zero_effect_values() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    calendar = Action(
        id="abcdef123456",
        type=ActionType.UPDATE_CALENDAR_EVENT,
        payload={
            "event_url": "https://calendar.example/event.ics",
            "location": "",
            "cancelled": False,
        },
        rationale="r",
    ).to_row()
    reminder = Action(
        id="abcdef123456",
        type=ActionType.CREATE_REMINDER,
        payload={
            "title": "Review me",
            "due": "2026-09-01T09:00:00+02:00",
            "priority": 0,
        },
        rationale="r",
    ).to_row()

    calendar_review = build_approval_review(calendar)
    reminder_review = build_approval_review(reminder)
    assert calendar_review.approvable is True
    assert "Location: (empty)" in calendar_review.summary_lines
    assert "Cancelled: false" in calendar_review.summary_lines
    assert reminder_review.approvable is True
    assert "Priority: 0" in reminder_review.summary_lines


def test_review_accepts_modern_task_nullable_fields() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.CREATE_TODOIST_TASK,
        payload={
            "content": "Task",
            "description": "",
            "project_id": "Family",
            "priority": None,
            "due_date": None,
            "due_string": None,
            "labels": None,
            "action_note_id": None,
        },
        rationale="r",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is True
    assert review.summary_lines == ("Task: Task", "List: Family")
    assert not any("null" in line for line in review.summary_lines)


def test_review_rejects_conflicting_task_due_fields() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.CREATE_TODOIST_TASK,
        payload={
            "content": "Task",
            "project_id": "Family",
            "due_date": "2026-08-25",
            "due_string": "tomorrow",
        },
        rationale="r",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "cannot combine due_date and due_string" in review.reason


def test_review_accepts_contact_name_patch_fields() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.UPDATE_CONTACT,
        payload={
            "href": "contacts/42.vcf",
            "given_name": "Jane",
            "family_name": "Doe",
        },
        rationale="r",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is True
    assert "Given name: Jane" in review.summary_lines
    assert "Family name: Doe" in review.summary_lines


def test_review_rejects_vcard_parameter_injection() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.CREATE_CONTACT,
        payload={
            "full_name": "Jane Doe",
            "emails": [{
                "value": "jane@example.com",
                "type": "HOME:\r\nNOTE:injected",
            }],
        },
        rationale="add contact",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert "invalid contact type" in review.reason


def test_review_rejects_origin_provenance_mismatch() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.PUSH_NOTIFICATION,
        payload={
            "title": "Title",
            "message": "Body",
            "area": "career",
            "agent": "career",
        },
        rationale="r",
        origin_agent="career",
        origin_area="family",
    ).to_row()

    review = build_approval_review(row)
    assert review.approvable is False
    assert review.reason == "queued area does not match action origin"


def test_occasion_reminder_review_discloses_exact_identity_and_effect() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    review = build_approval_review(Action(
        id="abcdef123456",
        type=ActionType.PUSH_NOTIFICATION,
        payload={
            "message": "Birthday reminder",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": 2026,
        },
        rationale="r",
    ).to_row())

    rendered = "\n".join(review.summary_lines)
    assert review.approvable
    assert "Occasion person: ravi" in rendered
    assert "Occasion: birthday" in rendered
    assert "Occasion year: 2026" in rendered
    assert "suppress this exact occasion reminder" in rendered


def test_review_keeps_unicode_and_newlines_but_exposes_invisible_controls() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    review = build_approval_review(Action(
        id="abcdef123456",
        type=ActionType.PUSH_NOTIFICATION,
        payload={
            "title": "Morning — München",
            "message": (
                "first\nsecond; literal \\n and \\u2014; "
                "<!channel>&; controls:\u0085\u2028; bidi:\u202e"
            ),
        },
        rationale="r",
    ).to_row())

    rendered = "\n".join(review.summary_lines)
    assert "Title: Morning — München" in rendered
    assert "Message: first\n  ↳ second" in rendered
    assert "literal \\n and \\u2014" in rendered
    assert "‹!channel›＆" in rendered
    assert "<!channel>" not in rendered
    assert "\u202e" not in rendered
    assert "\\u202e" in rendered
    assert "\\u0085" in rendered
    assert "\\u2028" in rendered


def test_occasion_reminder_review_rejects_partial_identity() -> None:
    from src.brain.actions.approval_review import build_approval_review
    from src.brain.actions.types import Action, ActionType

    review = build_approval_review(Action(
        id="abcdef123456",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"message": "Reminder", "occasion": "birthday"},
        rationale="r",
    ).to_row())

    assert not review.approvable
    assert "requires person, occasion, and year together" in review.reason


def test_human_approval_gate_refuses_unreviewable_row(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue
    from src.brain.actions.approval_review import ApprovalReview
    from src.brain.actions.types import Action, ActionType

    action_id = "abcdef123456"
    queue.insert(Action(
        id=action_id,
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Review me", "due": "2026-09-01T09:00:00+02:00"},
        rationale="r",
    ))
    monkeypatch.setattr(
        queue,
        "build_approval_review",
        lambda _row: ApprovalReview(False, reason="test integrity failure"),
    )

    assert queue.transition_status(
        action_id,
        "pending",
        "approved",
        channel="slack_button",
    ) is False
    assert queue.get(action_id)["status"] == "pending"
    assert queue.transition_status(
        action_id,
        "pending",
        "rejected",
        channel="slack_button",
    ) is True


def test_scoped_button_approval_requires_exact_delivered_review(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    action_id = "abcdef123456"
    queue.insert(Action(
        id=action_id,
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Review me", "due": "2026-09-01T09:00:00+02:00"},
        rationale="r",
        origin_area="family",
    ))
    assert not queue.transition_status(
        action_id, "pending", "approved", channel="slack_button"
    )
    row = queue.get(action_id)
    assert row and row["status"] == "pending"

    queue.record_review_delivery(
        action_id, row["payload_hash"], "family", "CFAMILY", "1.2"
    )
    assert queue.transition_status(
        action_id, "pending", "approved", channel="slack_button"
    )


def test_review_refuses_an_action_type_without_a_label(monkeypatch) -> None:
    from src.brain.actions import approval_review
    from src.brain.actions.types import Action, ActionType

    row = Action(
        id="abcdef123456",
        type=ActionType.UPDATE_CONTACT,
        payload={"href": "contacts/1.vcf", "note": "expected"},
        rationale="r",
    ).to_row()
    assert approval_review.build_approval_review(row).approvable is True

    labels = dict(approval_review._ACTION_LABELS)
    del labels[ActionType.UPDATE_CONTACT]
    monkeypatch.setattr(approval_review, "_ACTION_LABELS", labels)
    review = approval_review.build_approval_review(row)
    assert review.approvable is False
    assert review.reason == "queued action type is unsupported"


@pytest.mark.parametrize(("header", "label"), [
    # A made-up relay address, split so the personal-values scan reads no address here.
    ("The Owner <notifications_at_github_com_abc123def456_0000test" "@icloud.com>", "github.com"),
    ("Run Team <news@example.com>", "Run Team"),
    ("<alerts@bank.example>", "bank.example"),
    ("plain@example.org", "example.org"),
])
def test_a_sender_is_shown_by_the_site_behind_a_relay_or_by_name(header, label) -> None:
    from src.brain.actions.approval_review import sender_label

    assert sender_label(header) == label
