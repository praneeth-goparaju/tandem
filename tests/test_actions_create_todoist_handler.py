from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def test_handler_dedups_against_local_hash(isolated_db) -> None:
    from src.brain.actions.handlers.create_todoist_task import execute
    from src.brain.plugins.tasks._dedup import title_hash
    from src.db import get_db, utcnow

    conn = get_db()
    conn.execute(
        "INSERT INTO task_hashes (content_hash, normalized_title, original_title, todoist_id, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (
            title_hash("Submit application"),
            "submit application",
            "Submit application",
            "abc",
            utcnow(),
            "9999-12-31T23:59:59",
        ),
    )
    conn.commit()

    result = asyncio.run(execute({"content": "Submit application", "project_id": "p1"}))
    assert result["ok"] is True
    assert "DEDUP" in result["result"] or "dedup" in result["result"]


def test_handler_creates_when_no_match(isolated_db) -> None:
    from src.brain.actions.handlers import create_todoist_task as h

    fake_tasks = AsyncMock()
    fake_tasks.list_pending = AsyncMock(return_value=[])
    fake_tasks.list_completed = AsyncMock(return_value=[])
    fake_tasks.add = AsyncMock(return_value={"id": "999", "content": "Brand new"})

    with patch("src.brain.actions.handlers.create_todoist_task.tasks", return_value=fake_tasks):
        result = asyncio.run(h.execute({"content": "Brand new", "project_id": "p1"}))

    assert result["ok"] is True
    assert "created" in result["result"].lower()
    fake_tasks.add.assert_awaited_once()


def test_handler_returns_failure_when_todoist_errors(isolated_db) -> None:
    from src.brain.actions.handlers import create_todoist_task as h

    fake_tasks = AsyncMock()
    fake_tasks.list_pending = AsyncMock(side_effect=RuntimeError("Todoist 401"))

    with patch("src.brain.actions.handlers.create_todoist_task.tasks", return_value=fake_tasks):
        result = asyncio.run(h.execute({"content": "X", "project_id": "p1"}))

    assert result["ok"] is False
    assert "Todoist" in result["result"]
    assert result.get("retryable") is not True


def test_handler_marks_task_backend_timeout_retryable(isolated_db) -> None:
    from src.brain.actions.handlers import create_todoist_task as h

    fake_tasks = AsyncMock()
    fake_tasks.list_pending = AsyncMock(side_effect=TimeoutError("slow"))

    with patch("src.brain.actions.handlers.create_todoist_task.tasks", return_value=fake_tasks):
        result = asyncio.run(h.execute({"content": "X", "project_id": "p1"}))

    assert result["ok"] is False
    assert result["retryable"] is True


def test_handler_rejects_invalid_action_note_id_before_backend_call(isolated_db) -> None:
    from src.brain.actions.handlers import create_todoist_task as h

    fake_tasks = AsyncMock()
    with patch("src.brain.actions.handlers.create_todoist_task.tasks", return_value=fake_tasks):
        result = asyncio.run(
            h.execute(
                {
                    "content": "Brand new",
                    "project_id": "p1",
                    "action_note_id": "../not-an-action",
                }
            )
        )

    assert result == {"ok": False, "result": "invalid action_note_id"}
    fake_tasks.list_pending.assert_not_awaited()
    fake_tasks.add.assert_not_awaited()


def test_linked_task_dedup_does_not_create_an_ambiguous_association(
    isolated_db,
) -> None:
    from src.brain.actions.handlers import create_todoist_task as h

    deduped = AsyncMock(
        return_value={
            "ok": True,
            "deduped": True,
            "result": "DEDUP (fuzzy pending)",
        }
    )
    with patch.object(h, "dedup_and_add", deduped):
        result = asyncio.run(
            h.execute(
                {
                    "content": "Possibly similar task",
                    "project_id": "p1",
                    "action_note_id": "A-20260819-01",
                }
            )
        )

    assert result["ok"] is False
    assert "no exact backend task association" in result["result"]
    assert isolated_db.get_db().execute(
        "SELECT 1 FROM action_note_task_links"
    ).fetchone() is None
