from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

import pytest

from src.brain.mcp.tools import action_notes

_TASK_ID = "task-linked-123"


@pytest.fixture(autouse=True)
def _completed_task_link(isolated_db):
    from src.brain.actions import task_links

    task_links.link("A-20260819-01", _TASK_ID)
    task_links.mark_completed(_TASK_ID, "2026-08-23T09:00:00")


def _call(**arguments):
    from src.brain.mcp.caller_context import ToolCaller, caller_scope, current_caller

    arguments.setdefault("task_id", _TASK_ID)
    with caller_scope(current_caller() or ToolCaller(agent="housekeeping", area="housekeeping")):
        return asyncio.run(action_notes.action_note_complete(arguments))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _write_action(
    vault: Path,
    *,
    action_id: str = "A-20260819-01",
    status: str = "next",
    body: str = "# Pack the form\n\n## Outcome\nReady.\n\n## Steps\n1. Pack it.\n",
) -> Path:
    actions = vault / "Actions"
    actions.mkdir(parents=True, exist_ok=True)
    path = actions / f"{action_id} - Pack the form.md"
    path.write_text(
        "---\n"
        "type: action\n"
        f"action_id: {action_id}\n"
        f"status: {status}\n"
        "priority: high\n"
        "updated: 2026-08-19\n"
        "tags: [action, family]\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def test_completes_only_lifecycle_frontmatter_and_preserves_body(
    isolated_db, isolated_vault, monkeypatch
) -> None:
    path = _write_action(isolated_vault)
    before = path.read_text(encoding="utf-8")
    before_body = before.split("---\n", 2)[2]
    monkeypatch.setattr(
        action_notes, "_today_in_configured_timezone", lambda: "2026-08-23"
    )

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="The completed reminder covers the outcome and only step.",
    )

    after = path.read_text(encoding="utf-8")
    assert "ERROR:" not in _text(result)
    assert "status: done\n" in after
    assert "updated: 2026-08-23\n" in after
    assert "completed: 2026-08-23\n" in after
    assert after.split("---\n", 2)[2] == before_body
    assert "priority: high\n" in after
    assert "tags: [action, family]\n" in after
    event = isolated_db.get_db().execute(
        "SELECT actor, action, subject, trust, detail FROM events"
    ).fetchone()
    assert event is not None
    assert event[:4] == (
        "housekeeping",
        "action_note_completed",
        "A-20260819-01",
        "trusted",
    )
    assert "completed=2026-08-23" in event[4]
    assert "body=unchanged" in event[4]


def test_refuses_partial_completion_without_writing(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    before = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=False,
        rationale="One step remains.",
    )

    assert "ERROR:" in _text(result)
    assert "partial" in _text(result)
    assert path.read_bytes() == before


def test_refuses_unlinked_completed_task(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    before = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        task_id="not-linked",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="A description merely claimed this id.",
    )

    assert "durably linked" in _text(result)
    assert path.read_bytes() == before


@pytest.mark.parametrize("agent", ["router", "family"])
def test_only_the_housekeeping_run_can_complete_an_action_note(isolated_vault, agent) -> None:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    path = _write_action(isolated_vault)
    before = path.read_bytes()

    with caller_scope(ToolCaller(agent=agent, area=agent)):
        result = _call(
            action_id="A-20260819-01",
            completed_date="2026-08-23",
            settles_entire_outcome=True,
            rationale=f"The {agent} run tried to claim completion.",
        )

    assert "only the housekeeping run" in _text(result)
    assert path.read_bytes() == before


def test_terminal_note_is_idempotent_and_immutable(isolated_vault) -> None:
    path = _write_action(isolated_vault, status="done")
    before = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="Already complete.",
    )

    assert "already terminal" in _text(result)
    assert path.read_bytes() == before


def test_crlf_note_preserves_body_bytes_and_line_endings(
    isolated_db, isolated_vault, monkeypatch
) -> None:
    path = _write_action(isolated_vault)
    crlf = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    before_body = crlf.split(b"---\r\n", 2)[2]
    monkeypatch.setattr(
        action_notes, "_today_in_configured_timezone", lambda: "2026-08-23"
    )

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="The completed reminder settles the only step.",
    )

    after = path.read_bytes()
    assert "ERROR:" not in _text(result)
    assert after.split(b"---\r\n", 2)[2] == before_body
    assert b"status: done\r\n" in after
    assert b"completed: 2026-08-23\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")


def test_cancelled_note_cannot_be_reopened_as_done(isolated_vault) -> None:
    path = _write_action(isolated_vault, status="cancelled")
    before = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="A late task completion appeared.",
    )

    assert "already terminal" in _text(result)
    assert path.read_bytes() == before


def test_rejects_invalid_id_date_and_missing_rationale(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    before = path.read_bytes()

    invalid_id = _call(
        action_id="../A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )
    invalid_date = _call(
        action_id="A-20260819-01",
        completed_date="23-08-2026",
        settles_entire_outcome=True,
        rationale="x",
    )
    missing_reason = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale=" ",
    )
    future_date = _call(
        action_id="A-20260819-01",
        completed_date="2999-01-01",
        settles_entire_outcome=True,
        rationale="x",
    )
    oversized_reason = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x" * 501,
    )

    assert "ERROR:" in _text(invalid_id)
    assert "ERROR:" in _text(invalid_date)
    assert "ERROR:" in _text(missing_reason)
    assert "future" in _text(future_date)
    assert "at most 500" in _text(oversized_reason)
    assert path.read_bytes() == before


def test_rejects_ambiguous_or_mismatched_notes(isolated_vault) -> None:
    first = _write_action(isolated_vault)
    duplicate = first.with_name("A-20260819-01 - Duplicate.md")
    duplicate.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    ambiguous = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )
    assert "multiple action notes" in _text(ambiguous)

    duplicate.unlink()
    first.write_text(
        first.read_text(encoding="utf-8").replace(
            "action_id: A-20260819-01", "action_id: A-20260819-99"
        ),
        encoding="utf-8",
    )
    mismatch = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )
    assert "does not match" in _text(mismatch)


def test_rejects_actions_directory_symlink(isolated_vault, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _write_action(outside)
    (isolated_vault / "Actions").symlink_to(outside / "Actions", target_is_directory=True)
    before = target.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )

    assert "cannot be a symlink" in _text(result)
    assert target.read_bytes() == before


def test_rejects_action_note_symlink(isolated_vault) -> None:
    actions = isolated_vault / "Actions"
    actions.mkdir()
    outside = isolated_vault / "outside.md"
    outside.write_text(
        "---\ntype: action\naction_id: A-20260819-01\nstatus: next\n---\nbody\n",
        encoding="utf-8",
    )
    link = actions / "A-20260819-01 - Link.md"
    link.symlink_to(outside)
    before = outside.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )

    assert "symlinks are not supported" in _text(result)
    assert link.is_symlink()
    assert outside.read_bytes() == before


def test_refuses_busy_locked_action_note(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    before = path.read_bytes()
    lock_fd = os.open(path, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _call(
            action_id="A-20260819-01",
            completed_date="2026-08-23",
            settles_entire_outcome=True,
            rationale="x",
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert "action note is busy" in _text(result)
    assert path.read_bytes() == before


def test_detects_concurrent_change_before_atomic_replace(
    isolated_vault, monkeypatch
) -> None:
    path = _write_action(isolated_vault)
    real_write = action_notes.atomic_write_text_if_sha256

    def race_before_commit(target: Path, updated: str, expected: str) -> None:
        target.write_text(
            action_notes._read_text_exact(target).replace("Ready.", "Changed elsewhere."),
            encoding="utf-8",
        )
        real_write(target, updated, expected)

    monkeypatch.setattr(action_notes, "atomic_write_text_if_sha256", race_before_commit)
    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )

    assert "changed concurrently" in _text(result)
    assert "Changed elsewhere." in action_notes._read_text_exact(path)
    assert "status: next" in action_notes._read_text_exact(path)
    assert not list(path.parent.glob(".*.tmp"))


def test_rejects_duplicate_lifecycle_fields(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    before = path.read_text(encoding="utf-8")
    path.write_text(
        before.replace("status: next\n", "status: next\nstatus: waiting\n"),
        encoding="utf-8",
    )
    duplicated = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )

    assert "duplicate" in _text(result)
    assert "status" in _text(result)
    assert path.read_bytes() == duplicated


def test_rejects_quoted_or_semantically_duplicate_managed_keys(isolated_vault) -> None:
    path = _write_action(isolated_vault)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("status: next\n", '"status": next\n'),
        encoding="utf-8",
    )
    quoted = path.read_bytes()

    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )
    assert "canonical 'status'" in _text(result)
    assert path.read_bytes() == quoted

    path.write_text(
        original.replace("status: next\n", 'status: next\n"status": waiting\n'),
        encoding="utf-8",
    )
    duplicated = path.read_bytes()
    result = _call(
        action_id="A-20260819-01",
        completed_date="2026-08-23",
        settles_entire_outcome=True,
        rationale="x",
    )
    assert "duplicate key 'status'" in _text(result)
    assert path.read_bytes() == duplicated


