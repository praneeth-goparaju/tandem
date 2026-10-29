from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from src.brain.actions.alarms import (
    due_date_components,
    localize_datetime,
    validate_alarm_minutes,
)
from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    partial_success,
)

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_due(s: str) -> datetime:
    s = s.strip()
    if _DATE_ONLY_RE.fullmatch(s):
        return datetime.combine(date.fromisoformat(s), time(9, 0))
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_execute_sync, payload)
    except Exception as exc:  # noqa: BLE001
        return failure_from_exception("EventKit reminder setup failed", exc)


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    title = (payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "result": "missing title"}

    raw_due = (payload.get("due") or "").strip()
    if not raw_due:
        return {"ok": False, "result": "missing due; every reminder needs an alarm"}
    raw_alarm = (payload.get("alarm") or "").strip()
    try:
        due_local = localize_datetime(_parse_due(raw_due))
        alarm_minutes = validate_alarm_minutes(payload.get("alarm_minutes_before"))
        alarm_at = (
            localize_datetime(_parse_due(raw_alarm))
            if raw_alarm
            else due_local - timedelta(minutes=alarm_minutes)
        )
    except ValueError as exc:
        return {"ok": False, "result": f"bad reminder timing: {exc}"}

    raw_priority = payload.get("priority")
    if raw_priority is not None:
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            return {"ok": False, "result": f"bad priority {raw_priority!r}: must be int 0-9"}
        if not 0 <= priority <= 9:
            return {"ok": False, "result": f"bad priority {priority}: must be 0-9"}
    else:
        priority = None

    try:
        from src.brain.mcp.tools._icloud import eventkit_store
        store = eventkit_store()
    except Exception as e:  # noqa: BLE001
        return failure_from_exception("EventKit unavailable", e)

    from EventKit import EKAlarm, EKEntityTypeReminder, EKReminder

    requested_name = (payload.get("list_name") or "").strip()
    name_filter = requested_name.lower()
    calendars = store.calendarsForEntityType_(EKEntityTypeReminder)
    target = None
    if name_filter:
        for c in calendars:
            if str(c.title()).strip().lower() == name_filter:
                target = c
                break
        if target is None:
            existing = ", ".join(repr(str(c.title())) for c in calendars) or "none"
            return {
                "ok": False,
                "result": (
                    f"no reminder list named {requested_name!r}; lists that exist: {existing}"
                ),
            }
    else:
        target = store.defaultCalendarForNewReminders() or (calendars[0] if calendars else None)
    if target is None:
        return {"ok": False, "result": "no reminder lists available"}

    rem = EKReminder.reminderWithEventStore_(store)
    rem.setCalendar_(target)
    rem.setTitle_(title)
    if payload.get("notes"):
        rem.setNotes_(payload["notes"])
    if priority is not None:
        rem.setPriority_(priority)
    rem.setDueDateComponents_(due_date_components(due_local))
    rem.addAlarm_(EKAlarm.alarmWithAbsoluteDate_(alarm_at))

    try:
        saved, err = store.saveReminder_commit_error_(rem, True, None)
    except Exception as exc:  # noqa: BLE001
        return ambiguous_failure(
            "EventKit save outcome unknown; reconcile Reminders before retry: "
            f"{type(exc).__name__}: {exc}"
        )
    # PyObjC's saveReminder_commit_error_ can return (True, NSError) for partial
    # failures — checking only `saved` would swallow those.
    if not saved or err is not None:
        return ambiguous_failure(
            f"EventKit save outcome unknown; reconcile Reminders before retry: {err!r}"
        )

    try:
        requested = requested_name
        actual_cal = rem.calendar()
        actual_title = str(actual_cal.title()) if actual_cal is not None else ""
        target_title = str(target.title())
        reminder_id = rem.calendarItemIdentifier()
    except Exception as exc:  # noqa: BLE001
        return partial_success(
            "created reminder, but result metadata is unavailable: "
            f"{type(exc).__name__}: {exc}"
        )
    try:
        saved_item = store.calendarItemWithIdentifier_(reminder_id)
        saved_alarms = list(saved_item.alarms() or []) if saved_item is not None else []
        expected_ts = alarm_at.timestamp()
        alarm_verified = any(
            (timestamp := _absolute_alarm_timestamp(alarm)) is not None
            and abs(timestamp - expected_ts) <= 60
            for alarm in saved_alarms
        )
    except Exception:  # noqa: BLE001
        alarm_verified = False
    if not alarm_verified:
        return partial_success(
            f"created reminder id={reminder_id}, but its alarm could not be "
            "verified; inspect Reminders before relying on it"
        )
    if requested and actual_title and actual_title != target_title:
        return {
            "ok": True,
            "result": (
                f"created reminder id={reminder_id} "
                f"saved to {actual_title!r} (requested {requested!r})"
            ),
            "warning": "different_list",
        }
    alarm_label = alarm_at.isoformat() if raw_alarm else f"{alarm_minutes}m-before"
    return {
        "ok": True,
        "result": (
            f"created reminder id={reminder_id} list={actual_title or target_title} "
            f"alarm={alarm_label}"
        ),
    }


def _absolute_alarm_timestamp(alarm: Any) -> float | None:
    value = alarm.absoluteDate()
    if value is None:
        return None
    if isinstance(value, datetime):
        return localize_datetime(value).timestamp()
    interval = getattr(value, "timeIntervalSince1970", None)
    if callable(interval):
        return float(interval())
    return None
