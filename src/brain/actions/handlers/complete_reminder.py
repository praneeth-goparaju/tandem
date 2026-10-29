from __future__ import annotations

import asyncio
from typing import Any

from src.brain.actions.outcomes import ambiguous_failure, failure_from_exception


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_execute_sync, payload)
    except Exception as exc:  # noqa: BLE001
        return failure_from_exception("EventKit reminder completion setup failed", exc)


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    identifier = (payload.get("id") or "").strip()
    if not identifier:
        return {"ok": False, "result": "missing reminder id"}

    try:
        from src.brain.mcp.tools._icloud import eventkit_store

        store = eventkit_store()
    except Exception as exc:  # noqa: BLE001
        return failure_from_exception("EventKit unavailable", exc)

    reminder = store.calendarItemWithIdentifier_(identifier)
    if reminder is None:
        return {"ok": False, "result": f"reminder {identifier!r} not found"}

    title = str(reminder.title() or "(untitled)")
    if bool(reminder.isCompleted()):
        return {
            "ok": True,
            "result": f"reminder id={identifier} title={title!r} was already complete",
            "already_complete": True,
        }

    reminder.setCompleted_(True)
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

    saved_item = store.calendarItemWithIdentifier_(identifier)
    if saved_item is None or not bool(saved_item.isCompleted()):
        return {
            "ok": True,
            "partial": True,
            "result": (
                f"saved completion for reminder id={identifier}, but could not "
                "verify it; inspect Reminders before retrying"
            ),
        }
    return {
        "ok": True,
        "result": f"completed reminder id={identifier} title={title!r}",
    }
