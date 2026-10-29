from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

from vobject.icalendar import utc as _VOBJECT_UTC

from src.brain.actions.alarms import (
    DEFAULT_EVENT_ALERT_MINUTES,
    all_day_alarm_at,
    validate_alarm_minutes,
)
from src.brain.actions.outcomes import ambiguous_failure

# vobject's pickTzid can't serialize Python's `datetime.timezone.utc`
# (its `.dst()` returns None, not timedelta(0)) or bare offset tzinfo from
# `+02:00`-style ISO strings. Stamping `_VOBJECT_UTC` makes vobject emit
# `Z`-suffixed values; iCloud renders in user's local TZ regardless.


def _parse_dt(s: str, all_day: bool):
    if all_day:
        return date.fromisoformat(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"bad datetime {s!r}: {e}") from e
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_VOBJECT_UTC)
    return dt.astimezone(UTC).replace(tzinfo=_VOBJECT_UTC)


def alert_minutes(payload: dict[str, Any]) -> int:
    """Minutes before the start that an event alert fires.

    `alert_minutes` wins; `alarm_minutes_before` is the older name for the same
    thing. With neither, the alert is 60 minutes before.
    """
    if payload.get("alert_minutes") is not None:
        return validate_alarm_minutes(
            payload["alert_minutes"],
            default=DEFAULT_EVENT_ALERT_MINUTES,
            field="alert_minutes",
        )
    return validate_alarm_minutes(
        payload.get("alarm_minutes_before"), default=DEFAULT_EVENT_ALERT_MINUTES
    )


def add_alert(vev, summary: str, start, minutes: int) -> None:
    alarm = vev.add("valarm")
    alarm.add("action").value = "DISPLAY"
    alarm.add("description").value = summary
    trigger = alarm.add("trigger")
    if isinstance(start, date) and not isinstance(start, datetime):
        # An all-day event has no clock time, so the alert is set against the
        # local morning of its day.
        absolute = all_day_alarm_at(start, minutes)
        trigger.value = absolute.astimezone(UTC).replace(tzinfo=_VOBJECT_UTC)
    else:
        # Written as text so it reads -PT60M, not the -PT1H vobject would choose.
        trigger.value = f"-PT{minutes}M"
        trigger.isNative = False


def _build_vevent(payload: dict[str, Any], uid: str) -> str:
    import vobject

    cal = vobject.iCalendar()
    cal.add("prodid").value = "-//tandem//icloud handler//EN"

    vev = cal.add("vevent")
    vev.add("uid").value = uid
    vev.add("summary").value = payload["summary"]

    all_day = bool(payload.get("all_day"))
    start = _parse_dt(payload["start"], all_day)
    vev.add("dtstart").value = start
    vev.add("dtend").value = _parse_dt(payload["end"], all_day)

    if payload.get("location"):
        vev.add("location").value = payload["location"]
    if payload.get("description"):
        vev.add("description").value = payload["description"]
    for addr in payload.get("attendees") or []:
        att = vev.add("attendee")
        att.value = f"mailto:{addr}"
        att.params["RSVP"] = ["TRUE"]
    add_alert(vev, payload["summary"], start, alert_minutes(payload))
    vev.add("dtstamp").value = datetime.now(_VOBJECT_UTC)
    return cal.serialize()


def _calendar_display_name(calendar: Any) -> str:
    get_display_name = getattr(calendar, "get_display_name", None)
    if callable(get_display_name):
        return str(get_display_name() or "")
    return str(getattr(calendar, "name", "") or "")


def _pick_calendar(client, name_filter: str):
    principal = client.principal()
    cals = principal.calendars()
    if not cals:
        return None
    if name_filter:
        f = name_filter.strip().lower()
        for c in cals:
            if f in _calendar_display_name(c).lower():
                return c
        return None
    # Default: prefer the user's primary writable calendar. iCloud accounts
    # often list a shared read-only "Home" calendar first in the principal
    # listing — picking cals[0] blindly produces a 403 Forbidden on PUT.
    # Match preferred names in priority order, then fall back to the first cal.
    _DEFAULT_NAMES = ("icloud", "calendar")
    for preferred in _DEFAULT_NAMES:
        for c in cals:
            if _calendar_display_name(c).strip().lower() == preferred:
                return c
    return cals[0]


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    from src.brain.mcp.tools._icloud import ICloudNotConfigured, caldav_client

    for field in ("summary", "start", "end"):
        if not payload.get(field):
            return {"ok": False, "result": f"missing {field}"}

    try:
        client = caldav_client()
    except ICloudNotConfigured as e:
        return {"ok": False, "result": str(e)}

    cal = _pick_calendar(client, payload.get("calendar_name") or "")
    if cal is None:
        return {"ok": False, "result": "no matching calendar found"}

    action_id = str(payload.get("_action_id") or "").strip()
    generated = (
        uuid.uuid5(uuid.NAMESPACE_URL, f"brain-action:{action_id}")
        if action_id
        else uuid.uuid4()
    )
    uid = f"brain-{generated}"
    try:
        ics = _build_vevent(payload, uid)
    except ValueError as e:
        return {"ok": False, "result": str(e)}

    try:
        ev = cal.save_event(ics)
    except Exception as e:  # noqa: BLE001
        return ambiguous_failure(
            "CalDAV save outcome unknown; reconcile Calendar before retry: "
            f"{type(e).__name__}: {e}"
        )
    return {
        "ok": True,
        "result": (
            f"created event uid={uid} url={ev.url} "
            f"alarm={alert_minutes(payload)}m-before"
        ),
    }


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_execute_sync, payload)
