from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from src.brain.plugins.tasks import apple_reminders


class FakeNSDate:
    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def timeIntervalSince1970(self) -> float:
        return self._instant.timestamp()

    def __str__(self) -> str:
        return self._instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


class FakeCalendar:
    def title(self) -> str:
        return "Inbox"


class FakeReminder:
    def __init__(self, title: str, completed_at: FakeNSDate | None) -> None:
        self._title = title
        self._completed_at = completed_at

    def title(self) -> str:
        return self._title

    def notes(self) -> None:
        return None

    def calendar(self) -> FakeCalendar:
        return FakeCalendar()

    def priority(self) -> int:
        return 0

    def dueDateComponents(self) -> None:
        return None

    def calendarItemIdentifier(self) -> str:
        return f"id-{self._title}"

    def completionDate(self) -> FakeNSDate | None:
        return self._completed_at


class FakeStore:
    def __init__(self, reminders: list[FakeReminder]) -> None:
        self._reminders = reminders

    def calendarsForEntityType_(self, _kind: int) -> list[FakeCalendar]:
        return [FakeCalendar()]

    def predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
        self, _start, _end, _calendars
    ) -> object:
        return object()

    def fetchRemindersMatchingPredicate_completion_(self, _predicate, callback) -> None:
        callback(self._reminders)


def _nsdate(year: int, month: int, day: int, hour: int) -> FakeNSDate:
    return FakeNSDate(datetime(year, month, day, hour, tzinfo=UTC))


def test_completion_datetime_uses_berlin_date_at_local_midnight() -> None:
    raw = _nsdate(2026, 8, 23, 22)

    completed_at = apple_reminders._completion_datetime(
        raw, local_tz=ZoneInfo("Europe/Berlin")
    )

    assert str(raw).startswith("2026-08-23")
    assert completed_at is not None
    assert completed_at.isoformat() == "2026-08-24T00:00:00+02:00"
    assert apple_reminders._completion_datetime(None) is None


def test_completed_reminders_are_sorted_before_limit_and_none_is_last(monkeypatch) -> None:
    reminders = [
        FakeReminder("oldest", _nsdate(2026, 8, 20, 8)),
        FakeReminder("missing-date", None),
        FakeReminder("newest", _nsdate(2026, 8, 23, 9)),
        FakeReminder("middle", _nsdate(2026, 8, 22, 10)),
    ]
    store = FakeStore(reminders)
    monkeypatch.setattr(apple_reminders, "eventkit_store", lambda: store)
    monkeypatch.setattr(apple_reminders, "_nsdate_hours_ago", lambda _hours: object())
    monkeypatch.setattr(
        apple_reminders, "_configured_timezone", lambda: ZoneInfo("Europe/Berlin")
    )
    manager = apple_reminders.AppleRemindersTaskManager()

    limited = manager._list_completed_sync(limit=2)
    all_items = manager._list_completed_sync(limit=10)

    assert [item["content"] for item in limited] == ["newest", "middle"]
    assert [item["content"] for item in all_items] == [
        "newest",
        "middle",
        "oldest",
        "missing-date",
    ]
    assert all_items[-1]["completed_at"] == ""
    instants = [
        datetime.fromisoformat(item["completed_at"]).astimezone(UTC)
        for item in limited
    ]
    assert instants == [
        datetime(2026, 8, 23, 9, tzinfo=UTC),
        datetime(2026, 8, 22, 10, tzinfo=UTC),
    ]
    assert limited[0]["completed_at"].endswith("+02:00")
