from __future__ import annotations

import re

from src.db import get_db, utcnow

_ACTION_ID_RE = re.compile(r"^A-\d{8}-\d{2}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def link(action_id: str, task_id: str) -> None:
    action_id = str(action_id or "").strip().upper()
    task_id = str(task_id or "").strip()
    if not _ACTION_ID_RE.fullmatch(action_id) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid action-note/task link")
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO action_note_task_links "
        "(action_id, task_id, linked_at) VALUES (?,?,?)",
        (action_id, task_id, utcnow()),
    )
    conn.commit()


def mark_completed(task_id: str, completed_at: str | None = None) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE action_note_task_links SET completed_at=COALESCE(completed_at, ?) "
        "WHERE task_id=?",
        (completed_at or utcnow(), str(task_id)),
    )
    conn.commit()


def action_ids_for_task(task_id: str) -> list[str]:
    rows = get_db().execute(
        "SELECT action_id FROM action_note_task_links WHERE task_id=? "
        "ORDER BY action_id",
        (str(task_id),),
    ).fetchall()
    return [str(row[0]) for row in rows]


def is_completed_link(action_id: str, task_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM action_note_task_links "
        "WHERE action_id=? AND task_id=? AND completed_at IS NOT NULL",
        (str(action_id).upper(), str(task_id)),
    ).fetchone()
    return row is not None
