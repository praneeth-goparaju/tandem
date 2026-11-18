from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from src.brain.mcp.tools import icloud_read


class _Date:
    def __init__(self, when: datetime) -> None:
        self._when = when

    def timeIntervalSince1970(self) -> float:  # noqa: N802 - EventKit name
        return self._when.timestamp()


class _Components:
    def __init__(self, when: datetime) -> None:
        self._when = when

    def date(self) -> _Date:
        return _Date(self._when)


class _Calendar:
    def title(self) -> str:
        return "Family"


class _Reminder:
    def __init__(self, title: str, due: datetime | None) -> None:
        self._title = title
        self._due = due

    def title(self) -> str:
        return self._title

    def calendar(self) -> _Calendar:
        return _Calendar()

    def isCompleted(self) -> bool:  # noqa: N802
        return False

    def priority(self) -> int:
        return 0

    def dueDateComponents(self) -> _Components | None:  # noqa: N802
        return None if self._due is None else _Components(self._due)

    def alarms(self) -> list[Any]:
        return []

    def calendarItemIdentifier(self) -> str:  # noqa: N802
        return f"id-{self._title}"


NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _three() -> list[_Reminder]:
    return [
        _Reminder("tomorrow", NOW + timedelta(days=1)),
        _Reminder("last week", NOW - timedelta(days=7)),
        _Reminder("no due", None),
    ]


def test_ordered_for_listing_puts_overdue_first_and_undated_last() -> None:
    items = _three()

    ordered = icloud_read._ordered_for_listing(items, icloud_read._reminder_due_local)

    assert [r.title() for r in ordered] == ["last week", "tomorrow", "no due"]


def test_undated_reminders_keep_their_order() -> None:
    items = [_Reminder("b", None), _Reminder("a", None)]

    ordered = icloud_read._ordered_for_listing(items, icloud_read._reminder_due_local)

    assert [r.title() for r in ordered] == ["b", "a"]


def test_limit_one_returns_the_overdue_reminder(monkeypatch) -> None:
    class Store:
        def calendarsForEntityType_(self, kind):  # noqa: N802
            return [_Calendar()]

        def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(  # noqa: N802
            self, start, end, calendars
        ):
            return object()

        def fetchRemindersMatchingPredicate_completion_(self, predicate, callback):  # noqa: N802
            callback(_three())

    monkeypatch.setattr(icloud_read, "eventkit_store", lambda: Store())

    result = asyncio.run(icloud_read.reminders_list({"limit": 1}))

    text = result["content"][0]["text"]
    assert "last week" in text
    assert "tomorrow" not in text
