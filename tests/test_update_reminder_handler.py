from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timezone
from types import SimpleNamespace

import pytest

from src.brain.actions.handlers import update_reminder


class Alarm:
    def __init__(self, value):
        self._value = value

    @classmethod
    def alarmWithAbsoluteDate_(cls, value):
        return cls(value)

    def absoluteDate(self):
        return self._value


class Reminder:
    def __init__(self, *, alarms=()) -> None:
        self._title = "Call school"
        self._notes = "old notes"
        self._alarms = [Alarm(value) for value in alarms]
        self.components = None

    def title(self):
        return self._title

    def setTitle_(self, title):
        self._title = title

    def notes(self):
        return self._notes

    def setNotes_(self, notes):
        self._notes = notes

    def setDueDateComponents_(self, components):
        self.components = components

    def alarms(self):
        return list(self._alarms)

    def addAlarm_(self, alarm):
        self._alarms.append(alarm)

    def removeAlarm_(self, alarm):
        self._alarms.remove(alarm)


class Store:
    def __init__(self, reminder=None, *, keep_alarms: bool = True) -> None:
        self.reminder = reminder
        self.keep_alarms = keep_alarms
        self.saves = 0

    def calendarItemWithIdentifier_(self, _identifier):
        return self.reminder

    def saveReminder_commit_error_(self, reminder, *_args):
        self.saves += 1
        if not self.keep_alarms:
            reminder._alarms = []
        return True, None


class Components:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def __getattr__(self, name):
        if name.startswith("set"):
            key = name[3:-1].lower()
            return lambda value: setattr(self, key, value)
        raise AttributeError(name)


class LocalTimeZone:
    @staticmethod
    def localTimeZone():
        return object()


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setattr("src.orchestrator.config.TIMEZONE", "Europe/Berlin")


def _install(monkeypatch, store: Store) -> Store:
    monkeypatch.setitem(sys.modules, "EventKit", SimpleNamespace(EKAlarm=Alarm))
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(NSDateComponents=Components, NSTimeZone=LocalTimeZone),
    )
    monkeypatch.setattr("src.brain.mcp.tools._icloud.eventkit_store", lambda: store)
    return store


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_missing_id_fails_before_eventkit() -> None:
    result = asyncio.run(update_reminder.execute({"title": "x"}))
    assert result["ok"] is False
    assert "missing reminder id" in result["result"]


def test_update_unknown_id(monkeypatch) -> None:
    store = _install(monkeypatch, Store())

    result = asyncio.run(update_reminder.execute({"id": "missing", "title": "x"}))

    assert result["ok"] is False
    assert "not found" in result["result"]
    assert store.saves == 0


def test_nothing_to_change_is_refused(monkeypatch) -> None:
    store = _install(monkeypatch, Store(Reminder()))

    result = asyncio.run(update_reminder.execute({"id": "ABC-123"}))

    assert result["ok"] is False
    assert "nothing to update" in result["result"]
    assert store.saves == 0


def test_update_replaces_alarm(monkeypatch, berlin) -> None:
    reminder = Reminder(alarms=[_utc(2026, 5, 12, 6, 0), _utc(2026, 5, 11, 6, 0)])
    store = _install(monkeypatch, Store(reminder))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "due": "2026-05-14T10:00",
    }))

    assert result["ok"] is True
    assert store.saves == 1
    alarms = reminder.alarms()
    assert len(alarms) == 1
    assert alarms[0].absoluteDate().isoformat() == "2026-05-14T10:00:00+02:00"
    assert (reminder.components.day, reminder.components.hour) == (14, 10)


def test_update_explicit_alarm_wins_over_due(monkeypatch, berlin) -> None:
    reminder = Reminder(alarms=[_utc(2026, 5, 12, 6, 0)])
    _install(monkeypatch, Store(reminder))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "due": "2026-05-14T10:00", "alarm": "2026-05-13T20:00",
    }))

    assert result["ok"] is True
    alarms = reminder.alarms()
    assert len(alarms) == 1
    assert alarms[0].absoluteDate().isoformat() == "2026-05-13T20:00:00+02:00"


def test_update_alarm_only_keeps_due(monkeypatch, berlin) -> None:
    reminder = Reminder(alarms=[_utc(2026, 5, 12, 6, 0)])
    _install(monkeypatch, Store(reminder))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "alarm": "2026-05-12T07:30",
    }))

    assert result["ok"] is True
    assert reminder.components is None
    assert [a.absoluteDate().isoformat() for a in reminder.alarms()] == [
        "2026-05-12T07:30:00+02:00",
    ]


def test_update_title_and_notes_leaves_alarms(monkeypatch, berlin) -> None:
    original = _utc(2026, 5, 12, 6, 0)
    reminder = Reminder(alarms=[original])
    _install(monkeypatch, Store(reminder))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "title": "Call the school office", "notes": "ask about Friday",
    }))

    assert result["ok"] is True
    assert reminder.title() == "Call the school office"
    assert reminder.notes() == "ask about Friday"
    assert [a.absoluteDate() for a in reminder.alarms()] == [original]


def test_update_bad_timing_changes_nothing(monkeypatch, berlin) -> None:
    original = _utc(2026, 5, 12, 6, 0)
    reminder = Reminder(alarms=[original])
    store = _install(monkeypatch, Store(reminder))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "title": "new", "due": "next week",
    }))

    assert result["ok"] is False
    assert "bad reminder timing" in result["result"]
    assert store.saves == 0
    assert reminder.title() == "Call school"
    assert [a.absoluteDate() for a in reminder.alarms()] == [original]


def test_update_unverified_alarm_is_partial(monkeypatch, berlin) -> None:
    reminder = Reminder(alarms=[_utc(2026, 5, 12, 6, 0)])
    _install(monkeypatch, Store(reminder, keep_alarms=False))

    result = asyncio.run(update_reminder.execute({
        "id": "ABC-123", "due": "2026-05-14T10:00",
    }))

    assert result["ok"] is True
    assert result["partial"] is True
    assert "could not be verified" in result["result"]
