from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
from datetime import UTC, date, datetime, time, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.brain.mcp.tools._icloud import eventkit_store
from src.brain.plugins.base import TaskCreateOutcomeUnknown

log = logging.getLogger("brain.plugins.tasks.apple_reminders")

_EK_ENTITY_REMINDER = 1
_FETCH_TIMEOUT = 10.0
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FLEET_TO_EK = {1: 0, 2: 9, 3: 5, 4: 1}


def _reminder_class():
    from EventKit import EKReminder

    return EKReminder


def _ek_priority(p: Any) -> int:
    return _FLEET_TO_EK[max(1, min(4, int(p)))]


def _fleet_priority(ek: Any) -> int:
    ek = int(ek or 0)
    if ek == 0:
        return 1
    if ek <= 2:
        return 4
    if ek <= 5:
        return 3
    return 2


def _parse_due(s: str) -> datetime | None:
    s = s.strip()
    if _DATE_ONLY_RE.fullmatch(s):
        return datetime.combine(date.fromisoformat(s), time(9, 0))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _due_components(dt: datetime):
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    from Foundation import NSDateComponents, NSTimeZone

    comps = NSDateComponents.alloc().init()
    comps.setYear_(dt.year)
    comps.setMonth_(dt.month)
    comps.setDay_(dt.day)
    comps.setHour_(dt.hour)
    comps.setMinute_(dt.minute)
    comps.setTimeZone_(NSTimeZone.localTimeZone())
    return comps


def _components_date_str(comps) -> str | None:
    try:
        y, m, d = int(comps.year()), int(comps.month()), int(comps.day())
        if 0 < y < 10000:
            return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:  # noqa: BLE001
        pass
    nd = comps.date()
    return str(nd)[:10] if nd is not None else None


def _nsdate_hours_ago(hours: int):
    from Foundation import NSDate

    return NSDate.dateWithTimeIntervalSinceNow_(-float(hours) * 3600.0)


def _completion_datetime(
    value: Any, *, local_tz: tzinfo | None = None
) -> datetime | None:
    """Return an NSDate-like completion value as an aware local datetime.

    EventKit returns ``NSDate``, whose string representation is UTC.  Converting
    the instant before rendering keeps the date prefix aligned with the local
    Reminders calendar.  The datetime/string fallbacks keep test doubles and
    older bridge values compatible.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        instant = value
    else:
        interval = getattr(value, "timeIntervalSince1970", None)
        if callable(interval):
            try:
                instant = datetime.fromtimestamp(
                    float(interval()), tz=UTC
                )
            except (OSError, OverflowError, TypeError, ValueError):
                return None
        else:
            try:
                instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None

    if instant.tzinfo is None:
        instant = instant.astimezone()
    return instant.astimezone(local_tz) if local_tz is not None else instant.astimezone()


def _configured_timezone() -> tzinfo:
    from src.orchestrator import config

    try:
        return ZoneInfo(config.TIMEZONE)
    except ZoneInfoNotFoundError:
        log.warning(
            "apple_reminders: invalid TIMEZONE %r — falling back to UTC",
            config.TIMEZONE,
        )
        return UTC


def _resolve_list(store, name: str | None):
    calendars = list(store.calendarsForEntityType_(_EK_ENTITY_REMINDER) or [])
    if name:
        needle = name.strip().lower()
        for c in calendars:
            if needle in str(c.title()).lower():
                return c
        raise RuntimeError(f"no Reminders list matching {name!r}")
    default = store.defaultCalendarForNewReminders()
    if default is not None:
        return default
    if calendars:
        return calendars[0]
    raise RuntimeError("no Reminders lists available")


def _save(store, rem) -> None:
    try:
        saved, err = store.saveReminder_commit_error_(rem, True, None)
    except Exception as exc:  # noqa: BLE001
        raise TaskCreateOutcomeUnknown(
            f"EventKit save raised after dispatch: {type(exc).__name__}: {exc}"
        ) from exc
    # PyObjC's saveReminder_commit_error_ can return (True, NSError) for
    # partial failures. In either failure shape the reminder may exist even
    # though EventKit did not return a usable acknowledgement.
    if not saved or err is not None:
        raise TaskCreateOutcomeUnknown(
            f"EventKit save outcome unknown (saved={bool(saved)}, error={err!r})"
        )


def _fetch(store, predicate) -> list:
    done = threading.Event()
    bucket: dict[str, list] = {"items": []}

    def cb(items):
        bucket["items"] = list(items or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
    if not done.wait(timeout=_FETCH_TIMEOUT):
        raise RuntimeError(f"EventKit fetch timed out after {_FETCH_TIMEOUT:.0f}s")
    return bucket["items"]


def _to_dict(rem) -> dict[str, Any]:
    title = str(rem.title() or "")
    cal = rem.calendar()
    due = None
    comps = rem.dueDateComponents()
    if comps is not None:
        ds = _components_date_str(comps)
        if ds:
            due = {"date": ds}
    return {
        "id": str(rem.calendarItemIdentifier()),
        "content": title,
        "title": title,
        "project_id": str(cal.title()) if cal is not None else "",
        "priority": _fleet_priority(rem.priority()),
        "due": due,
        "description": str(rem.notes() or ""),
    }


async def _run(fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


class AppleRemindersTaskManager:
    async def add(
        self,
        title: str,
        *,
        description: str = "",
        project: str | None = None,
        priority: int | None = None,
        due: str | None = None,
        labels: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await _run(
            self._add_sync,
            title,
            description=description,
            project=project,
            priority=priority,
            due=due,
            labels=labels,
        )

    def _add_sync(
        self,
        title: str,
        *,
        description: str = "",
        project: str | None = None,
        priority: int | None = None,
        due: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        store = eventkit_store()
        target = _resolve_list(store, project)
        rem = _reminder_class().reminderWithEventStore_(store)
        rem.setCalendar_(target)
        rem.setTitle_(title)
        notes = description or ""
        if labels:
            notes = f"{notes}\n\nlabels: {', '.join(labels)}".strip()
        if notes:
            rem.setNotes_(notes)
        if priority is not None:
            rem.setPriority_(_ek_priority(priority))
        if due:
            dt = _parse_due(str(due))
            if dt is None:
                log.warning(
                    "apple_reminders: unparseable due %r — creating without due date", due
                )
            else:
                rem.setDueDateComponents_(_due_components(dt))
        _save(store, rem)
        return {"id": str(rem.calendarItemIdentifier()), "url": None}

    async def list_pending(self, *, project: str | None = None) -> list[dict[str, Any]]:
        return await _run(self._list_pending_sync, project=project)

    def _list_pending_sync(self, *, project: str | None = None) -> list[dict[str, Any]]:
        store = eventkit_store()
        calendars = list(store.calendarsForEntityType_(_EK_ENTITY_REMINDER) or [])
        if project:
            needle = project.strip().lower()
            calendars = [c for c in calendars if needle in str(c.title()).lower()]
            if not calendars:
                return []
        pred = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
            None, None, calendars
        )
        return [_to_dict(r) for r in _fetch(store, pred)]

    async def list_completed(
        self, *, since_hours: int = 24, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await _run(self._list_completed_sync, since_hours=since_hours, limit=limit)

    def _list_completed_sync(
        self, *, since_hours: int = 24, limit: int = 50
    ) -> list[dict[str, Any]]:
        store = eventkit_store()
        calendars = list(store.calendarsForEntityType_(_EK_ENTITY_REMINDER) or [])
        start = _nsdate_hours_ago(since_hours)
        pred = store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
            start, None, calendars
        )
        local_tz = _configured_timezone()
        reminders = [
            (rem, _completion_datetime(rem.completionDate(), local_tz=local_tz))
            for rem in _fetch(store, pred)
        ]
        reminders.sort(
            key=lambda item: item[1].timestamp() if item[1] is not None else float("-inf"),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for rem, completed_at in reminders[: max(0, int(limit))]:
            d = _to_dict(rem)
            d["completed_at"] = completed_at.isoformat() if completed_at is not None else ""
            out.append(d)
        return out
