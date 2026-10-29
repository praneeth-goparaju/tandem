from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.brain.actions.handlers.create_reminder import _parse_due, execute


def test_parse_due_aware_offset_preserved() -> None:
    dt = _parse_due("2026-05-12T14:00:00+02:00")
    assert dt.utcoffset() == timedelta(hours=2)


def test_parse_due_z_suffix_becomes_utc() -> None:
    dt = _parse_due("2026-05-12T14:00:00Z")
    assert dt.utcoffset() == timedelta(0)


def test_parse_due_date_only_assumes_9am_local_naive() -> None:
    dt = _parse_due("2026-05-12")
    assert dt == datetime(2026, 5, 12, 9, 0)
    assert dt.tzinfo is None


def test_parse_due_garbage_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_due("not a date")


def test_execute_missing_title_errors() -> None:
    result = asyncio.run(execute({}))
    assert result["ok"] is False
    assert "missing title" in result["result"]


def test_execute_bad_priority_string_errors() -> None:
    result = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "priority": "abc",
    }))
    assert result["ok"] is False
    assert "bad priority" in result["result"]


def test_execute_bad_priority_out_of_range_errors() -> None:
    result = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "priority": 12,
    }))
    assert result["ok"] is False
    assert "bad priority" in result["result"] and "0-9" in result["result"]


def test_execute_negative_priority_errors() -> None:
    result = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "priority": -1,
    }))
    assert result["ok"] is False
    assert "bad priority" in result["result"]


def test_execute_dispatches_eventkit_work_to_thread(monkeypatch) -> None:
    from src.brain.actions.handlers import create_reminder as handler

    event_loop_thread = threading.get_ident()

    def fake_execute_sync(payload):
        assert payload == {"title": "x"}
        return {
            "ok": True,
            "result": str(threading.get_ident()),
        }

    monkeypatch.setattr(handler, "_execute_sync", fake_execute_sync)
    result = asyncio.run(handler.execute({"title": "x"}))

    assert result["ok"] is True
    assert int(result["result"]) != event_loop_thread


def test_execute_marks_eventkit_oserror_retryable() -> None:
    with patch(
        "src.brain.mcp.tools._icloud.eventkit_store",
        side_effect=OSError("temporarily unavailable"),
    ):
        result = asyncio.run(execute({
            "title": "x", "due": "2026-05-12T09:00:00+02:00",
        }))

    assert result["ok"] is False
    assert result["retryable"] is True


def test_execute_marks_unclassified_worker_timeout_retryable(monkeypatch) -> None:
    from src.brain.actions.handlers import create_reminder as handler

    def fail(_payload):
        raise TimeoutError("worker timed out")

    monkeypatch.setattr(handler, "_execute_sync", fail)
    result = asyncio.run(handler.execute({"title": "x"}))

    assert result["ok"] is False
    assert result["retryable"] is True
    assert result.get("ambiguous") is not True
    assert "setup failed" in result["result"]


def test_execute_marks_save_exception_ambiguous(monkeypatch) -> None:
    from src.brain.actions.handlers import create_reminder as handler

    class Calendar:
        def title(self):
            return "Reminders"

    class Store:
        def calendarsForEntityType_(self, _entity_type):
            return [Calendar()]

        def defaultCalendarForNewReminders(self):
            return Calendar()

        def saveReminder_commit_error_(self, *_args):
            raise TimeoutError("save response lost")

    class Reminder:
        @classmethod
        def reminderWithEventStore_(cls, _store):
            return cls()

        def setCalendar_(self, _calendar):
            pass

        def setTitle_(self, _title):
            pass

        def setDueDateComponents_(self, _components):
            pass

        def addAlarm_(self, _alarm):
            pass

    class Alarm:
        @classmethod
        def alarmWithAbsoluteDate_(cls, value):
            return value

    class Components:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def __getattr__(self, name):
            if name.startswith("set"):
                return lambda _value: None
            raise AttributeError(name)

    class LocalTimeZone:
        @staticmethod
        def localTimeZone():
            return object()

    monkeypatch.setitem(
        sys.modules, "EventKit", SimpleNamespace(EKAlarm=Alarm, EKEntityTypeReminder=1, EKReminder=Reminder),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(NSDateComponents=Components, NSTimeZone=LocalTimeZone),
    )
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store",
        lambda: Store(),
    )

    result = asyncio.run(handler.execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00",
    }))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True
    assert "reconcile Reminders" in result["result"]


def test_execute_missing_due_errors() -> None:
    result = asyncio.run(execute({"title": "x"}))

    assert result["ok"] is False
    assert "missing due" in result["result"]


def _install_successful_eventkit(
    monkeypatch, *, verify_alarm: bool = True, lists: tuple[str, ...] = ("Family",),
):
    class Calendar:
        def __init__(self, name):
            self._name = name

        def title(self):
            return self._name

    class Alarm:
        def __init__(self, value):
            self._value = value

        @classmethod
        def alarmWithAbsoluteDate_(cls, value):
            return cls(value)

        def absoluteDate(self):
            return self._value

    class Reminder:
        def __init__(self):
            self._calendar = None
            self._alarms = []

        @classmethod
        def reminderWithEventStore_(cls, _store):
            return cls()

        def setCalendar_(self, calendar):
            self._calendar = calendar

        def calendar(self):
            return self._calendar

        def setTitle_(self, _title):
            pass

        def setNotes_(self, _notes):
            pass

        def setPriority_(self, _priority):
            pass

        def setDueDateComponents_(self, components):
            self.components = components

        def addAlarm_(self, alarm):
            self._alarms.append(alarm)

        def alarms(self):
            return self._alarms if verify_alarm else []

        def calendarItemIdentifier(self):
            return "reminder-1"

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

    class Store:
        def __init__(self):
            self.reminder = None

        def calendarsForEntityType_(self, _entity_type):
            return [Calendar(name) for name in lists]

        def defaultCalendarForNewReminders(self):
            return Calendar(lists[0])

        def saveReminder_commit_error_(self, reminder, *_args):
            self.reminder = reminder
            return True, None

        def calendarItemWithIdentifier_(self, _identifier):
            return self.reminder

    store = Store()
    monkeypatch.setitem(
        sys.modules, "EventKit", SimpleNamespace(EKAlarm=Alarm, EKEntityTypeReminder=1, EKReminder=Reminder),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(NSDateComponents=Components, NSTimeZone=LocalTimeZone),
    )
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store", lambda: store,
    )
    return store


def test_execute_adds_and_verifies_absolute_alarm(monkeypatch) -> None:
    store = _install_successful_eventkit(monkeypatch)

    result = asyncio.run(execute({
        "title": "Call school",
        "due": "2026-05-12T09:00:00+02:00",
        "alarm_minutes_before": 30,
    }))

    assert result["ok"] is True
    alarm_at = store.reminder.alarms()[0].absoluteDate()
    assert alarm_at.isoformat() == "2026-05-12T08:30:00+02:00"


def test_execute_saved_but_unverified_alarm_is_partial(monkeypatch) -> None:
    _install_successful_eventkit(monkeypatch, verify_alarm=False)

    result = asyncio.run(execute({
        "title": "Call school", "due": "2026-05-12",
    }))

    assert result["ok"] is True
    assert result["partial"] is True
    assert "could not be verified" in result["result"]


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setattr("src.orchestrator.config.TIMEZONE", "Europe/Berlin")


def test_due_gets_absolute_alarm_at_due(monkeypatch, berlin) -> None:
    store = _install_successful_eventkit(monkeypatch)

    result = asyncio.run(execute({"title": "Call school", "due": "2026-05-12T09:00"}))

    assert result["ok"] is True
    alarms = store.reminder.alarms()
    assert len(alarms) == 1
    assert alarms[0].absoluteDate().isoformat() == "2026-05-12T09:00:00+02:00"


def test_explicit_alarm_used(monkeypatch, berlin) -> None:
    store = _install_successful_eventkit(monkeypatch)

    result = asyncio.run(execute({
        "title": "Call school",
        "due": "2026-05-12T09:00",
        "alarm": "2026-05-11T18:15",
        "alarm_minutes_before": 30,
    }))

    assert result["ok"] is True
    alarms = store.reminder.alarms()
    assert len(alarms) == 1
    assert alarms[0].absoluteDate().isoformat() == "2026-05-11T18:15:00+02:00"
    assert "2026-05-11T18:15:00+02:00" in result["result"]


def test_bad_alarm_is_refused(monkeypatch, berlin) -> None:
    store = _install_successful_eventkit(monkeypatch)

    result = asyncio.run(execute({
        "title": "Call school", "due": "2026-05-12T09:00", "alarm": "tomorrow-ish",
    }))

    assert result["ok"] is False
    assert "bad reminder timing" in result["result"]
    assert store.reminder is None


def test_list_name_exact_not_substring(monkeypatch) -> None:
    store = _install_successful_eventkit(
        monkeypatch, lists=("Project Daily Tasks", "Tasks", "List A"),
    )

    first = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "list_name": "  Tasks ",
    }))
    assert first["ok"] is True
    assert store.reminder.calendar().title() == "Tasks"

    second = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "list_name": "list a",
    }))
    assert second["ok"] is True
    assert store.reminder.calendar().title() == "List A"


def test_list_name_without_exact_match_names_existing_lists(monkeypatch) -> None:
    store = _install_successful_eventkit(monkeypatch, lists=("Project Daily Tasks", "List A"))

    result = asyncio.run(execute({
        "title": "x", "due": "2026-05-12T09:00:00+02:00", "list_name": "Tasks",
    }))

    assert result["ok"] is False
    assert "Project Daily Tasks" in result["result"]
    assert "List A" in result["result"]
    assert store.reminder is None


def test_dst_due_uses_berlin_wall_time(monkeypatch, berlin) -> None:
    from zoneinfo import ZoneInfo

    store = _install_successful_eventkit(monkeypatch)

    result = asyncio.run(execute({"title": "x", "due": "2026-10-25T02:30"}))

    assert result["ok"] is True
    # The clocks go back on 25 Oct 2026, so 02:30 happens twice; the first one
    # (summer time, fold=0) is used.
    expected = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Berlin"), fold=0)
    alarm_at = store.reminder.alarms()[0].absoluteDate()
    assert alarm_at.timestamp() == expected.timestamp()
    assert alarm_at.astimezone(UTC) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC,
    )
    components = store.reminder.components
    assert (components.hour, components.minute, components.day) == (2, 30, 25)
