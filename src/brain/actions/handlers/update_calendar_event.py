from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, time
from datetime import date as _date
from types import ModuleType
from typing import Any, cast

from vobject.icalendar import utc as _VOBJECT_UTC

from src.brain.actions.alarms import (
    DEFAULT_EVENT_ALERT_MINUTES,
    configured_timezone,
    validate_alarm_minutes,
)
from src.brain.actions.handlers.create_calendar_event import _parse_dt, add_alert
from src.brain.actions.outcomes import ambiguous_failure, failure_from_exception
from src.brain.mcp.tools._icloud import assert_icloud_host


def _set_or_add(vev, attr: str, value) -> None:
    (getattr(vev, attr) if hasattr(vev, attr) else vev.add(attr)).value = value


_PATCHABLE_FIELDS = ("summary", "start", "end", "location", "description", "cancelled")


def _coerce(s: str, existing) -> Any:
    if isinstance(existing, _date) and not isinstance(existing, datetime):
        return _parse_dt(s, all_day=True)
    return _parse_dt(s, all_day=False)


def _apply_patch(vev, payload: dict[str, Any]) -> None:
    if "summary" in payload:
        _set_or_add(vev, "summary", payload["summary"])
    if "start" in payload:
        old_start = vev.dtstart.value
        new_start = _coerce(payload["start"], old_start)
        if (
            isinstance(old_start, _date)
            and not isinstance(old_start, datetime)
            and isinstance(new_start, _date)
            and not isinstance(new_start, datetime)
        ):
            _move_all_day_absolute_alarms(vev, old_start, new_start)
        vev.dtstart.value = new_start
    if "end" in payload:
        vev.dtend.value = _coerce(payload["end"], vev.dtend.value)
    if "location" in payload:
        _set_or_add(vev, "location", payload["location"])
    if "description" in payload:
        _set_or_add(vev, "description", payload["description"])
    if "cancelled" in payload:
        _set_or_add(vev, "status", "CANCELLED" if payload["cancelled"] else "CONFIRMED")
    minutes = _alert_minutes(payload)
    if minutes is not None:
        vev.contents.pop("valarm", None)
        summary = str(vev.summary.value) if hasattr(vev, "summary") else ""
        add_alert(vev, summary, vev.dtstart.value, minutes)


def _alert_minutes(payload: dict[str, Any]) -> int | None:
    if payload.get("alert_minutes") is None:
        return None
    return validate_alarm_minutes(
        payload["alert_minutes"], default=DEFAULT_EVENT_ALERT_MINUTES, field="alert_minutes"
    )


def _move_all_day_absolute_alarms(vev, old_start: _date, new_start: _date) -> None:
    tz = configured_timezone()
    old_anchor = datetime.combine(old_start, time.min, tzinfo=tz)
    new_anchor = datetime.combine(new_start, time.min, tzinfo=tz)
    for alarm in getattr(vev, "valarm_list", []):
        trigger = getattr(alarm, "trigger", None)
        if trigger is None:
            continue
        value = getattr(trigger, "value", None)
        if not isinstance(value, datetime):
            continue
        local_value = (
            value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
        )
        moved = new_anchor + (local_value - old_anchor)
        trigger.value = moved.astimezone(UTC).replace(tzinfo=_VOBJECT_UTC)


def _is_precondition_failed(exc: BaseException) -> bool:
    caldav_error: ModuleType | None
    try:
        caldav_error = importlib.import_module("caldav.lib.error")
    except Exception:  # noqa: BLE001
        caldav_error = None
    if caldav_error is not None:
        # caldav 3.x raises ETagMismatchError (not a PutError) for a 412 on an
        # object loaded with an ETag; older versions raise PutError.
        conflict_types = tuple(
            t
            for t in (
                getattr(caldav_error, "PutError", None),
                getattr(caldav_error, "ETagMismatchError", None),
                getattr(caldav_error, "ScheduleTagMismatchError", None),
            )
            if t is not None
        )
        if not isinstance(exc, conflict_types):
            return False
        if not isinstance(exc, getattr(caldav_error, "PutError", ())):
            return True
    if getattr(exc, "reason", None) == 412:
        return True
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) == 412


def _event_at(client, url: str):
    """Build a caldav Event for an existing URL, with its calendar as parent.

    caldav 3.x save() resolves the event path against the parent calendar's URL,
    so an Event without a parent fails with "'NoneType' object has no attribute 'url'".
    """
    from caldav import Calendar, Event

    # caldav's dynamic exports are typed as object rather than as classes.
    calendar_cls, event_cls = cast(Any, Calendar), cast(Any, Event)
    calendar_url = url.rsplit("/", 1)[0] + "/"
    return event_cls(client=client, url=url, parent=calendar_cls(client=client, url=calendar_url))


def _event_involves_others_sync(event_url: str) -> bool:
    import vobject

    from src.brain.mcp.tools._icloud import caldav_client

    event = _event_at(caldav_client(), assert_icloud_host(event_url.strip()))
    event.load()
    cal = vobject.readOne(event.data)
    return any(
        getattr(vevent, "attendee_list", None) or hasattr(vevent, "organizer")
        for vevent in getattr(cal, "vevent_list", [])
    )


async def event_involves_others(event_url: str) -> bool:
    """True when the event has attendees or an organizer, or cannot be checked.

    An update to such an event can reach other people, so it keeps its approval tap.
    """
    try:
        return await asyncio.to_thread(_event_involves_others_sync, event_url)
    except Exception:  # noqa: BLE001
        return True


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    import vobject

    from src.brain.mcp.tools._icloud import ICloudNotConfigured, caldav_client

    raw_url = (payload.get("event_url") or "").strip()
    if not raw_url:
        return {"ok": False, "result": "missing event_url"}
    try:
        url = assert_icloud_host(raw_url)
    except ValueError as e:
        return {"ok": False, "result": str(e)}
    try:
        alert = _alert_minutes(payload)
    except ValueError as e:
        return {"ok": False, "result": str(e)}
    if alert is None and not any(f in payload for f in _PATCHABLE_FIELDS):
        return {"ok": False, "result": (
            "no fields to update — provide at least one of "
            f"{', '.join(_PATCHABLE_FIELDS)}, alert_minutes"
        )}

    try:
        client = caldav_client()
    except ICloudNotConfigured as e:
        return {"ok": False, "result": str(e)}

    for attempt in (1, 2):
        try:
            ev = _event_at(client, url)
            ev.load()
        except Exception as e:  # noqa: BLE001
            return failure_from_exception("CalDAV fetch failed", e)

        cal = vobject.readOne(ev.data)
        if not hasattr(cal, "vevent") or len(cal.vevent_list) > 1:
            return {
                "ok": False,
                "result": "recurring event with overrides; not yet supported",
            }
        vev = cal.vevent
        try:
            _apply_patch(vev, payload)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "result": f"patch failed: {e}"}

        ev.data = cal.serialize()
        try:
            ev.save()
            return {"ok": True, "result": f"updated event url={url}"}
        except Exception as e:  # noqa: BLE001
            if _is_precondition_failed(e) and attempt == 1:
                continue
            if _is_precondition_failed(e):
                return {
                    "ok": False,
                    "result": "CalDAV update lost two ETag races — manual retry needed",
                }
            return ambiguous_failure(
                "CalDAV update outcome unknown; reconcile Calendar before retry: "
                f"{type(e).__name__}: {e}"
            )

    return {"ok": False, "result": "CalDAV update lost two ETag races — manual retry needed"}


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_execute_sync, payload)
