import asyncio
from datetime import date

from src.brain.mcp.tools import icloud_read


class _Calendar:
    name = "Home"

    def __init__(self, seen):
        self.seen = seen

    def search(self, *, start, end, event, expand):
        self.seen.append((start, end))
        return []


def _run(monkeypatch, args):
    seen = []

    class _Principal:
        def calendars(self):
            return [_Calendar(seen)]

    class _Client:
        def principal(self):
            return _Principal()

    monkeypatch.setattr(icloud_read, "caldav_client", lambda: _Client())
    result = asyncio.run(icloud_read.calendar_list_events(args))
    return result, seen


def test_the_same_day_twice_means_that_whole_day(monkeypatch):
    result, seen = _run(monkeypatch, {"date_from": "2026-10-18", "date_to": "2026-10-18"})
    assert not result.get("is_error"), result
    [(start, end)] = seen
    assert (start, end) == icloud_read._local_day_window(date(2026, 10, 18), date(2026, 10, 19))


def test_an_end_before_the_start_is_refused(monkeypatch):
    result, seen = _run(monkeypatch, {"date_from": "2026-10-18", "date_to": "2026-10-17"})
    assert result["is_error"] and seen == []
