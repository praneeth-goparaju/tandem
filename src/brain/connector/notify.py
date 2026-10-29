"""Reach the owner's iPhone from a job on the Mac mini.

``notify`` creates an Apple Reminder with an alarm a minute ahead in the named
list. Reminders notifications reliably reach the phone and stay until they are
ticked off. Between 21:30 and 06:00 local time the alarm waits until 06:00.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.brain.actions.handlers import create_reminder
from src.brain.connector.config import system_timezone

_logger = logging.getLogger(__name__)

QUIET_START = time(21, 30)
QUIET_END = time(6, 0)
EVENTKIT_TIMEOUT_SECONDS = 20.0
_LOG_NOTES_CHARS = 80
OFF_RESULT = {"ok": False, "result": "reminders integration is off"}


def alert_time(now: datetime, tz: str) -> datetime:
    """One minute after ``now``, or 06:00 local time when that falls in quiet hours.

    A naive ``now`` is local time in ``tz``; an aware one is converted. The result is aware.
    """
    zone = ZoneInfo(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    ahead = (now.astimezone(UTC) + timedelta(minutes=1)).astimezone(zone)
    wall = ahead.time()
    if wall >= QUIET_START:
        morning = ahead.date() + timedelta(days=1)
    elif wall < QUIET_END:
        morning = ahead.date()
    else:
        return ahead
    return datetime.combine(morning, QUIET_END, tzinfo=zone)


async def _create(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            create_reminder.execute(payload), timeout=EVENTKIT_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return {"ok": False, "result": "timed out"}


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    # A private loop closed without waiting for its worker thread: asyncio.run
    # would wait up to five minutes for a stuck EventKit call after the timeout.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_create(payload))
    finally:
        loop.close()


def notify(
    title: str,
    list_name: str,
    notes: str = "",
    now: datetime | None = None,
    *,
    enabled: bool = True,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Create a reminder in ``list_name`` that alerts about a minute from now. Never raises.

    Plain and safe to call from any thread that has no running event loop.
    """
    if not enabled:
        _logger.warning("reminders integration is off, alert not sent: %s", title)
        return dict(OFF_RESULT)
    try:
        zone = timezone or system_timezone()
        when = alert_time(now or datetime.now(UTC), zone).isoformat()
        payload = {
            "title": title,
            "notes": notes,
            "list_name": list_name,
            "due": when,
            "alarm": when,
        }
        result = _run(payload)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "result": str(exc) or type(exc).__name__}
    if not result.get("ok"):
        _logger.warning(
            "reminder not created (%s): %s | %s",
            result.get("result"),
            title,
            notes[:_LOG_NOTES_CHARS],
        )
    return result
