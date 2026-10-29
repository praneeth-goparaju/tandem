from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from src.brain.actions.alarms import due_date_components, localize_datetime
from src.brain.actions.handlers.create_reminder import _absolute_alarm_timestamp, _parse_due
from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    partial_success,
)


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_execute_sync, payload)
    except Exception as exc:  # noqa: BLE001
        return failure_from_exception("EventKit reminder update setup failed", exc)


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    identifier = (payload.get("id") or "").strip()
    if not identifier:
        return {"ok": False, "result": "missing reminder id"}

    title = payload.get("title")
    notes = payload.get("notes")
    raw_due = (payload.get("due") or "").strip()
    raw_alarm = (payload.get("alarm") or "").strip()
    if title is not None and not str(title).strip():
        return {"ok": False, "result": "title cannot be empty"}
    if title is None and notes is None and not raw_due and not raw_alarm:
        return {"ok": False, "result": "nothing to update; give title, notes, due or alarm"}

    due_local: datetime | None = None
    alarm_at: datetime | None = None
    try:
        if raw_due:
            due_local = localize_datetime(_parse_due(raw_due))
            alarm_at = due_local
        if raw_alarm:
            alarm_at = localize_datetime(_parse_due(raw_alarm))
    except ValueError as exc:
        return {"ok": False, "result": f"bad reminder timing: {exc}"}

    try:
        from src.brain.mcp.tools._icloud import eventkit_store

        store = eventkit_store()
    except Exception as exc:  # noqa: BLE001
        return failure_from_exception("EventKit unavailable", exc)

    reminder = store.calendarItemWithIdentifier_(identifier)
    if reminder is None:
        return {"ok": False, "result": f"reminder {identifier!r} not found"}

    changed: list[str] = []
    if title is not None:
        reminder.setTitle_(str(title).strip())
        changed.append("title")
    if notes is not None:
        reminder.setNotes_(str(notes))
        changed.append("notes")
    if due_local is not None:
        reminder.setDueDateComponents_(due_date_components(due_local))
        changed.append("due")
    if alarm_at is not None:
        from EventKit import EKAlarm

        for old in list(reminder.alarms() or []):
            reminder.removeAlarm_(old)
        reminder.addAlarm_(EKAlarm.alarmWithAbsoluteDate_(alarm_at))
        changed.append("alarm")

    try:
        saved, err = store.saveReminder_commit_error_(reminder, True, None)
    except Exception as exc:  # noqa: BLE001
        return ambiguous_failure(
            "EventKit save outcome unknown; inspect Reminders before retry: "
            f"{type(exc).__name__}: {exc}"
        )
    if not saved or err is not None:
        return ambiguous_failure(
            "EventKit save outcome unknown; inspect Reminders before retry: "
            f"{err!r}"
        )

    summary = f"updated reminder id={identifier} ({', '.join(changed)})"
    try:
        saved_item = store.calendarItemWithIdentifier_(identifier)
        verified = saved_item is not None
        if verified and title is not None:
            verified = str(saved_item.title()) == str(title).strip()
        if verified and notes is not None:
            verified = str(saved_item.notes() or "") == str(notes)
        if verified and alarm_at is not None:
            timestamps = [
                _absolute_alarm_timestamp(alarm) for alarm in saved_item.alarms() or []
            ]
            verified = len(timestamps) == 1 and (
                timestamps[0] is not None and abs(timestamps[0] - alarm_at.timestamp()) <= 60
            )
    except Exception:  # noqa: BLE001
        verified = False
    if not verified:
        return partial_success(
            f"{summary}, but the change could not be verified; "
            "inspect Reminders before retrying"
        )
    return {"ok": True, "result": summary}
