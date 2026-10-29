from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_ALARM_MINUTES_BEFORE = 0
DEFAULT_EVENT_ALERT_MINUTES = 60
MAX_ALARM_MINUTES_BEFORE = 7 * 24 * 60
ALL_DAY_ALARM_TIME = time(9, 0)


def validate_alarm_minutes(
    value: object,
    *,
    default: int = DEFAULT_ALARM_MINUTES_BEFORE,
    field: str = "alarm_minutes_before",
) -> int:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_ALARM_MINUTES_BEFORE
    ):
        raise ValueError(
            f"{field} must be an integer between 0 and {MAX_ALARM_MINUTES_BEFORE}"
        )
    return value


def configured_timezone() -> ZoneInfo:
    from src.orchestrator import config

    try:
        return ZoneInfo(str(config.TIMEZONE))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid configured timezone {config.TIMEZONE!r}") from exc


def localize_datetime(value: datetime) -> datetime:
    tz = configured_timezone()
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def all_day_alarm_at(day: date, minutes_before: int) -> datetime:
    local_morning = datetime.combine(day, ALL_DAY_ALARM_TIME, tzinfo=configured_timezone())
    return local_morning - timedelta(minutes=minutes_before)


def due_date_components(due_local: datetime) -> Any:
    from Foundation import NSDateComponents, NSTimeZone

    comps = NSDateComponents.alloc().init()
    comps.setYear_(due_local.year)
    comps.setMonth_(due_local.month)
    comps.setDay_(due_local.day)
    comps.setHour_(due_local.hour)
    comps.setMinute_(due_local.minute)
    comps.setTimeZone_(NSTimeZone.localTimeZone())
    return comps
