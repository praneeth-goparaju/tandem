from __future__ import annotations

import icalendar

from src.brain.mcp.tools.icloud_read import _format_event_line

ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:test-1\r\n"
    "SUMMARY:Yoga class\r\n"
    "DTSTART:20260902T130000Z\r\n"
    "DTEND:20260902T140000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

URL = "https://p1-caldav.icloud.com/1/calendars/home/test-1.ics"


class IcalendarInstanceEvent:
    url = URL

    def __init__(self) -> None:
        self.icalendar_instance = icalendar.Calendar.from_ical(ICS)


class DataOnlyEvent:
    url = URL
    data = ICS


def test_icalendar_instance_gives_real_summary_and_times():
    line = _format_event_line(IcalendarInstanceEvent(), "List B")
    assert "Yoga class" in line
    assert "(no title)" not in line
    assert "? → ?" not in line
    assert "2026-09-02T13:00:00" in line
    assert "2026-09-02T14:00:00" in line
    assert line.endswith(URL)


def test_data_fallback_still_parses_with_vobject():
    line = _format_event_line(DataOnlyEvent(), "List B")
    assert "Yoga class" in line
    assert "(no title)" not in line
    assert "? → ?" not in line


def test_unparseable_event_is_labelled_not_crashed():
    class Nothing:
        url = URL

    line = _format_event_line(Nothing(), "List B")
    assert "unparseable event" in line


def test_day_window_uses_local_midnight_not_utc():
    from datetime import date, time

    from src.brain.mcp.tools.icloud_read import _local_day_window

    start, end = _local_day_window(date(2026, 8, 27), date(2026, 9, 3))

    assert start.time() == time.min
    assert end.time() == time.min
    assert start.date() == date(2026, 8, 27)
    assert end.date() == date(2026, 9, 3)

    from datetime import datetime

    all_day_on_end = datetime.combine(date(2026, 9, 3), time.min, tzinfo=end.tzinfo)
    assert not (start <= all_day_on_end < end)

    early = datetime.combine(date(2026, 8, 27), time(1, 0), tzinfo=start.tzinfo)
    assert start <= early < end


def test_day_window_follows_dst():
    from datetime import date

    from src.brain.mcp.tools.icloud_read import _local_day_window

    winter_start, _ = _local_day_window(date(2026, 1, 15), date(2026, 1, 16))
    summer_start, _ = _local_day_window(date(2026, 7, 15), date(2026, 7, 16))
    assert winter_start.utcoffset() != summer_start.utcoffset()
