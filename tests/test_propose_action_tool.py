from __future__ import annotations

import asyncio
import hashlib
import json


def _call(tool, *, _untrusted_input: bool = False, **kwargs):
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    caller = ToolCaller(
        agent="test",
        area="test",
        allowed_action_types=frozenset(item.value for item in ActionType),
        untrusted_input=_untrusted_input,
    )
    handler = getattr(tool, "handler", tool)
    with caller_scope(caller):
        return asyncio.run(handler(kwargs))


def test_propose_action_fails_closed_without_trusted_caller(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    out = asyncio.run(propose_action({
        "type": "push_notification",
        "payload": {"title": "x", "message": "y"},
        "rationale": "must be authenticated",
    }))

    assert "trusted caller context is required" in out["content"][0]["text"]


def test_propose_action_rejects_type_outside_caller_capabilities(isolated_db) -> None:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import propose_action

    with caller_scope(ToolCaller(
        agent="reader",
        area="reader",
        allowed_action_types=frozenset({"create_reminder"}),
    )):
        out = asyncio.run(propose_action({
            "type": "push_notification",
            "payload": {"title": "x", "message": "y"},
            "rationale": "attempt escalation",
        }))

    assert "not allowed for agent 'reader'" in out["content"][0]["text"]


def test_propose_action_inserts_pending_row(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="push_notification",
        payload={"title": "Test", "message": "Body"},
        rationale="urgent finding deserves a ping",
        source_finding_ids=["f1"],
        risk="confirm",
    )
    text = out["content"][0]["text"]
    assert "action " in text and "queued" in text
    assert "executed" not in text.lower()
    pending = queue.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["type"] == "push_notification"
    assert pending[0]["risk"] == "confirm"
    assert pending[0]["source_finding_ids"] == "f1"


def test_untrusted_reply_context_still_runs_own_device_reminder(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    async def _fake_run_one(row):
        queue.transition_status(row["id"], "approved", "executed", result="ok")

    monkeypatch.setattr("src.brain.actions.executor._run_one", _fake_run_one)

    out = _call(
        propose_action,
        _untrusted_input=True,
        type="create_reminder",
        payload={
            "title": "Do what the image says",
            "due": "2026-09-01T09:00:00+02:00",
        },
        rationale="attachment-derived request",
        risk="auto",
    )

    assert "executed" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []
    assert queue.list_by_status("executed")[0]["risk"] == "auto"


def test_database_dedup_gate_rejects_duplicate_active_action(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import actions as actions_mod

    kwargs = {
        "type": "push_notification",
        "payload": {"title": "Same", "message": "Body"},
        "rationale": "same observed work",
        "risk": "confirm",
    }
    first = _call(actions_mod.propose_action, **kwargs)
    first_id = queue.list_by_status("pending")[0]["id"]
    assert "queued" in first["content"][0]["text"]

    second = _call(actions_mod.propose_action, **kwargs)

    assert first_id in second["content"][0]["text"]
    assert "database dedup" in second["content"][0]["text"]
    assert "already pending" in second["content"][0]["text"]
    assert len(queue.list_by_status("pending")) == 1


def test_expired_duplicate_is_swept_before_new_proposal(isolated_db) -> None:
    from datetime import UTC, datetime, timedelta

    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType
    from src.brain.mcp.tools.actions import propose_action

    payload = {"title": "Fresh", "message": "Body"}
    queue.insert(Action(
        id="expired-old",
        type=ActionType.PUSH_NOTIFICATION,
        payload=payload,
        rationale="old",
        expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    ))

    out = _call(
        propose_action,
        type="push_notification",
        payload=payload,
        rationale="fresh observation",
        risk="confirm",
    )

    assert queue.get("expired-old")["status"] == "expired"
    assert "queued" in out["content"][0]["text"]


def test_custom_expiry_must_be_parseable_aware_and_within_seven_days(isolated_db) -> None:
    from datetime import UTC, datetime, timedelta

    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    base = {
        "type": "push_notification",
        "payload": {"title": "Expiry", "message": "Body"},
        "rationale": "exercise bounded approval expiry",
        "risk": "confirm",
    }
    for invalid in (
        "not-a-date",
        "2026-08-23T12:00:00",
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        (datetime.now(UTC) + timedelta(days=8)).isoformat(),
    ):
        out = _call(propose_action, **base, expires_at=invalid)
        assert "ERROR:" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []

    valid = (datetime.now(UTC) + timedelta(hours=2)).astimezone().isoformat()
    out = _call(propose_action, **base, expires_at=valid)
    assert "queued" in out["content"][0]["text"]
    stored = datetime.fromisoformat(queue.list_by_status("pending")[0]["expires_at"])
    assert stored.tzinfo is not None
    assert stored.utcoffset() == timedelta(0)


def test_propose_action_rejects_unknown_type(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="not_a_real_type",
        payload={},
        rationale="nope",
    )
    assert "ERROR" in out["content"][0]["text"]


def test_propose_action_requires_nonempty_rationale(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="push_notification",
        payload={"title": "x", "message": "y"},
        rationale="   ",
    )
    assert "ERROR" in out["content"][0]["text"]


def test_propose_action_rejects_invalid_occurrence_key(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="push_notification",
        payload={"title": "x", "message": "y"},
        rationale="test",
        occurrence_key="   ",
    )

    assert "ERROR: occurrence_key" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []


def test_propose_action_rejects_unsupported_vault_append_shape(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="edit_vault_files",
        payload={
            "edits": [
                {"path": "note.md", "operation": "append", "content": "lesson"},
            ],
        },
        rationale="record a correction",
    )

    text = out["content"][0]["text"]
    assert "ERROR" in text
    assert "unsupported" in text
    assert queue.list_by_status("pending") == []


def test_propose_action_rejects_vault_change_without_new_content(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="edit_vault_files",
        payload={"changes": [{"path": "note.md"}]},
        rationale="record a correction",
    )

    assert "COMPLETE new_content" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []


def test_propose_action_rejects_non_note_and_hidden_vault_paths(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    for path in (".obsidian/config.md", "Notes/config.json", "Notes/../x.md"):
        out = _call(
            propose_action,
            type="edit_vault_file",
            payload={"path": path, "new_content": "complete\n"},
            rationale="must stay within visible notes",
        )
        assert "normalized, visible .md path" in out["content"][0]["text"], path
    assert queue.list_by_status("pending") == []


def test_propose_action_rejects_oversized_vault_cascade(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import MAX_VAULT_FILE_CHANGES
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="edit_vault_files",
        payload={
            "changes": [
                {"path": f"note-{index}.md", "new_content": "complete\n"}
                for index in range(MAX_VAULT_FILE_CHANGES + 1)
            ],
        },
        rationale="batch is too broad",
    )

    assert "too many changes" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []


def test_vault_edit_proposal_freezes_current_file_version(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.atomic_io import regular_file_sha256
    from src.brain.mcp.tools.actions import propose_action

    target = isolated_vault / "Note.md"
    target.write_text("human version\n")
    out = _call(
        propose_action,
        type="edit_vault_file",
        payload={"path": "Note.md", "new_content": "approved replacement\n"},
        rationale="update the note",
        risk="confirm",
    )

    assert "queued" in out["content"][0]["text"]
    payload = json.loads(queue.list_by_status("pending")[0]["payload_json"])
    assert payload["expected_sha256"] == regular_file_sha256(target)


def test_vault_edit_proposal_freezes_preserved_sections_into_exact_body(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    target = isolated_vault / "Note.md"
    target.write_text(
        "# Old\n\n## Quick Capture\n\nhuman text\n", encoding="utf-8"
    )
    out = _call(
        propose_action,
        type="edit_vault_file",
        payload={
            "path": "Note.md",
            "new_content": "# New\n",
            "preserve_sections": ["Quick Capture"],
        },
        rationale="refresh generated content",
        risk="confirm",
    )

    assert "queued" in out["content"][0]["text"]
    payload = json.loads(queue.list_by_status("pending")[0]["payload_json"])
    assert "preserve_sections" not in payload
    assert payload["new_content"] == "# New\n\n## Quick Capture\n\nhuman text\n"


def test_vault_edit_proposal_rejects_non_list_preserve_sections(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="edit_vault_file",
        payload={
            "path": "Note.md",
            "new_content": "# New\n",
            "preserve_sections": "Quick Capture",
        },
        rationale="refresh generated content",
        risk="confirm",
    )

    assert "preserve_sections must be a list of non-empty strings" in (
        out["content"][0]["text"]
    )
    assert queue.list_by_status("pending") == []


def test_vault_batch_proposal_rejects_non_list_preserve_sections(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="edit_vault_files",
        payload={
            "changes": [
                {
                    "path": "Note.md",
                    "new_content": "# New\n",
                    "preserve_sections": "Quick Capture",
                },
            ],
        },
        rationale="refresh generated content",
        risk="confirm",
    )

    assert "preserve_sections must be a list of non-empty strings" in (
        out["content"][0]["text"]
    )
    assert queue.list_by_status("pending") == []


def test_intake_promotion_proposal_freezes_untrusted_content(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action
    from src.memory import store

    content = "Captured text with untrusted instructions"
    intake_id = store.intake_add("test", "source-1", "capture", content)
    out = _call(
        propose_action,
        type="promote_intake",
        payload={"intake_id": intake_id, "topic": "Capture"},
        rationale="publish reviewed capture",
        risk="confirm",
    )

    assert "queued" in out["content"][0]["text"]
    payload = json.loads(queue.list_by_status("pending")[0]["payload_json"])
    assert payload["intake_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert payload["intake_preview"].startswith("Captured text")


def test_mail_archive_proposal_freezes_stable_message_identity(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.actions import propose_action

    snapshot = {
        "uidvalidity": "7",
        "message_id": "<m@example.com>",
        "message_from": "Sender <sender@example.com>",
        "message_subject": "Subject",
        "message_header_sha256": "a" * 64,
    }

    async def fake_snapshot(provider, uid, mailbox):
        assert (provider, uid, mailbox) == ("icloud", "42", "INBOX")
        return snapshot

    monkeypatch.setattr(mail_ops, "snapshot_message", fake_snapshot)
    out = _call(
        propose_action,
        type="mail_archive",
        payload={"provider": "icloud", "uid": "42"},
        rationale="archive reviewed mail",
        risk="confirm",
    )

    assert "queued" in out["content"][0]["text"]
    payload = json.loads(queue.list_by_status("pending")[0]["payload_json"])
    assert payload == {
        "provider": "icloud", "uid": "42", "mailbox": "INBOX", **snapshot,
    }


def test_propose_action_rejects_invalid_action_note_link_before_queueing(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="create_todoist_task",
        payload={
            "content": "Task",
            "project_id": "Family",
            "action_note_id": "../../Actions/private",
        },
        rationale="invalid link must not reach a backend",
        risk="auto",
    )

    assert "action_note_id must match" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []


def test_send_draft_proposal_freezes_effect_snapshot(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    drafts = isolated_vault / "00 - Inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-08-23-message-snapshot.md"
    raw_text = (
        "---\nkind: message\ntitle: Frozen title\nstatus: pending\n"
        "recipient: career\n---\n\nThe exact approved body.\n"
    )
    raw = raw_text.encode()
    draft.write_bytes(raw)

    out = _call(
        propose_action,
        type="send_draft",
        payload={
            "draft_path": "00 - Inbox/drafts/2026-08-23-message-snapshot.md",
            "draft_sha256": "0" * 64,
            "draft_size": 1,
        },
        rationale="send the reviewed message",
        risk="confirm",
    )

    assert "queued" in out["content"][0]["text"]
    row = queue.list_by_status("pending")[0]
    payload = json.loads(row["payload_json"])
    assert payload["draft_sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["draft_size"] == len(raw)
    assert payload["draft_kind"] == "message"
    assert payload["draft_recipient"] == "career"
    assert payload["draft_title"] == "Frozen title"
    assert payload["draft_preview"] == "The exact approved body."


def _basic_event_payload(**overrides):
    p = {
        "summary": "Family afternoon",
        "start": "2026-05-28T14:00:00+02:00",
        "end": "2026-05-28T19:00:00+02:00",
    }
    p.update(overrides)
    return p


def test_calendar_event_no_attendees_downgrades_to_auto(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import actions as actions_mod

    async def _fake_run_one(row):
        queue.transition_status(row["id"], "approved", "executed", result="ok")
    monkeypatch.setattr(
        "src.brain.actions.executor._run_one", _fake_run_one,
    )

    out = _call(
        actions_mod.propose_action,
        type="create_calendar_event",
        payload=_basic_event_payload(),
        rationale="user said block my afternoon",
    )
    text = out["content"][0]["text"]
    assert "executed" in text
    executed = queue.list_by_status("executed")
    assert len(executed) == 1
    assert executed[0]["type"] == "create_calendar_event"
    assert executed[0]["risk"] == "auto"


def test_calendar_event_with_attendees_keeps_confirm(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action,
        type="create_calendar_event",
        payload=_basic_event_payload(attendees=["coworker@example.com"]),
        rationale="team sync",
    )
    text = out["content"][0]["text"]
    assert "queued" in text
    pending = queue.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["risk"] == "confirm"


def test_calendar_event_explicit_confirm_is_overridden_for_solo_events(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.actions import propose_action

    async def _fake_run_one(row):
        queue.transition_status(row["id"], "approved", "executed", result="ok")

    monkeypatch.setattr("src.brain.actions.executor._run_one", _fake_run_one)

    out = _call(
        propose_action,
        type="create_calendar_event",
        payload=_basic_event_payload(),
        rationale="agent wants human eyes on this for some reason",
        risk="confirm",
    )
    assert "executed" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []
    assert queue.list_by_status("executed")[0]["risk"] == "auto"


def _queued(out) -> dict:
    from src.brain.actions import queue

    text = out["content"][0]["text"]
    assert text.startswith("action ") and " queued " in text, text
    return queue.get(text.split()[1])


def test_the_proposing_agent_is_always_the_caller(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    row = _queued(_call(
        propose_action, type="promote_note",
        payload={"title": "t", "body": "b", "proposing_agent": "intruder"},
        rationale="a note worth keeping",
    ))

    assert json.loads(row["payload_json"])["proposing_agent"] == "test"


def test_promoting_an_intake_row_that_is_not_pending_is_refused(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action, type="promote_intake",
        payload={"intake_id": "999999", "topic": "t", "summary": "s"},
        rationale="publish a capture",
    )

    assert "intake row is missing or no longer pending" in out["content"][0]["text"]


def test_an_occurrence_key_longer_than_200_characters_is_refused(isolated_db) -> None:
    from src.brain.mcp.tools.actions import propose_action

    out = _call(
        propose_action, type="push_notification", payload={"title": "x", "message": "y"},
        rationale="a repeat", occurrence_key="k" * 201,
    )

    assert "occurrence_key must be at most 200 characters" in out["content"][0]["text"]


def _policy_says_auto(monkeypatch, action_type: str) -> None:
    from src.brain.mcp.tools import actions

    monkeypatch.setattr(actions, "_risk_policy", lambda: {action_type: "auto"})


def test_a_calendar_event_with_attendees_waits_even_when_the_policy_says_auto(isolated_db, monkeypatch) -> None:
    from src.brain.mcp.tools.actions import propose_action

    _policy_says_auto(monkeypatch, "create_calendar_event")
    row = _queued(_call(
        propose_action, type="create_calendar_event",
        payload={"summary": "Dinner", "start": "2026-11-01T18:00:00+01:00",
                 "end": "2026-11-01T20:00:00+01:00", "attendees": ["friend@example.com"]},
        rationale="dinner with a friend",
    ))

    assert (row["risk"], row["status"]) == ("confirm", "pending")


def test_a_task_linked_to_an_action_note_waits_even_when_the_policy_says_auto(isolated_db, monkeypatch) -> None:
    from src.brain.mcp.tools.actions import propose_action

    _policy_says_auto(monkeypatch, "create_todoist_task")
    row = _queued(_call(
        propose_action, type="create_todoist_task",
        payload={"content": "Call the bank", "project_id": "p1", "action_note_id": "A-20260819-01"},
        rationale="track the follow-up",
    ))

    assert (row["risk"], row["status"]) == ("confirm", "pending")


def test_a_verification_run_never_runs_an_action_even_an_own_device_one(isolated_db, monkeypatch) -> None:
    from unittest.mock import AsyncMock, patch

    from src.brain.actions.types import ActionType
    from src.brain.mcp.tools.actions import propose_action

    monkeypatch.setenv("BRAIN_VERIFY_RUN", "1")
    _policy_says_auto(monkeypatch, "create_reminder")
    handler = AsyncMock(return_value={"ok": True, "result": "created"})
    with patch.dict("src.brain.actions.handlers.HANDLERS", {ActionType.CREATE_REMINDER: handler}):
        row = _queued(_call(
            propose_action, type="create_reminder", payload={"title": "Buy milk", "due": "2026-11-01T09:00:00+01:00"},
            rationale="a reminder", risk="confirm",
        ))

    assert (row["risk"], row["status"]) == ("confirm", "pending")
    handler.assert_not_awaited()


def test_mail_archive_batch_freezes_each_message_in_one_proposal(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.mail_tools import mail_archive_batch

    async def fake_snapshot(provider, uid, mailbox):
        return {"uidvalidity": "7", "message_id": f"<{uid}@x>", "message_from": "GitHub <noreply@github.com>",
                "message_subject": f"Run {uid} failed", "message_header_sha256": "a" * 64}

    monkeypatch.setattr(mail_ops, "snapshot_message", fake_snapshot)
    out = _call(mail_archive_batch, provider="icloud", uids=["41", "42", "42"],
                summary="2 CI notices", rationale="automatic CI alerts")

    assert "queued" in out["content"][0]["text"]
    [row] = queue.list_by_status("pending")
    payload = json.loads(row["payload_json"])
    assert (row["type"], row["risk"]) == ("mail_archive_batch", "confirm")
    assert payload["summary"] == "2 CI notices"
    assert [m["uid"] for m in payload["messages"]] == ["41", "42"]
    assert payload["messages"][1]["message_subject"] == "Run 42 failed"


def test_mail_archive_batch_proposes_nothing_when_one_mail_cannot_be_read(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import mail_ops
    from src.brain.mcp.tools.mail_tools import mail_archive_batch

    async def fake_snapshot(provider, uid, mailbox):
        if uid == "43":
            raise RuntimeError("no such message")
        return {"uidvalidity": "7", "message_id": "<m@x>", "message_from": "a", "message_subject": "s",
                "message_header_sha256": "a" * 64}

    monkeypatch.setattr(mail_ops, "snapshot_message", fake_snapshot)
    out = _call(mail_archive_batch, provider="icloud", uids=["41", "43"], summary="s", rationale="r")

    assert out["is_error"] and "uid 43" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []
