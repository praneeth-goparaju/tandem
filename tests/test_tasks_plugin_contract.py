from __future__ import annotations

import asyncio
import importlib.util
from unittest.mock import AsyncMock, patch

import pytest

from src.brain.plugins.base import TaskManager


class FakeCalendar:
    def __init__(self, title: str) -> None:
        self._title = title

    def title(self) -> str:
        return self._title


class FakeReminder:
    _counter = 0

    def __init__(self) -> None:
        FakeReminder._counter += 1
        self._id = f"FAKE-{FakeReminder._counter:04d}-UUID"
        self._title = ""
        self._notes = None
        self._priority = 0
        self._calendar = None
        self._completed = False
        self._due = None
        self._completion_date = None

    def setTitle_(self, t):
        self._title = t

    def setNotes_(self, n):
        self._notes = n

    def setPriority_(self, p):
        self._priority = p

    def setCalendar_(self, c):
        self._calendar = c

    def setCompleted_(self, v):
        self._completed = bool(v)
        self._completion_date = "2026-07-07 12:00:00 +0000" if v else None

    def setDueDateComponents_(self, c):
        self._due = c

    def title(self):
        return self._title

    def notes(self):
        return self._notes

    def priority(self):
        return self._priority

    def calendar(self):
        return self._calendar

    def isCompleted(self):
        return self._completed

    def dueDateComponents(self):
        return self._due

    def completionDate(self):
        return self._completion_date

    def calendarItemIdentifier(self):
        return self._id


class FakeStore:
    def __init__(self, calendars=()) -> None:
        self._cals = list(calendars)
        self._reminders: list[FakeReminder] = []
        self.saved: list[FakeReminder] = []
        self.save_result = (True, None)

    def calendarsForEntityType_(self, kind):
        return list(self._cals)

    def defaultCalendarForNewReminders(self):
        return self._cals[0] if self._cals else None

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, s, e, cals):
        return ("incomplete", tuple(c.title() for c in cals))

    def predicateForRemindersInCalendars_(self, cals):
        return ("all", tuple(c.title() for c in cals))

    def predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
        self, s, e, cals
    ):
        return ("completed", tuple(c.title() for c in cals))

    def fetchRemindersMatchingPredicate_completion_(self, pred, cb):
        kind, cal_titles = pred
        pool = [
            r for r in self._reminders
            if r.calendar() is None or r.calendar().title() in cal_titles
        ]
        if kind == "incomplete":
            cb([r for r in pool if not r.isCompleted()])
        elif kind == "completed":
            cb([r for r in pool if r.isCompleted()])
        else:
            cb(pool)

    def calendarItemWithIdentifier_(self, ident):
        for r in self._reminders:
            if r.calendarItemIdentifier() == ident:
                return r
        return None

    def saveReminder_commit_error_(self, rem, commit, err):
        if rem not in self._reminders:
            self._reminders.append(rem)
        self.saved.append(rem)
        return self.save_result


class FakeReminderClass:
    @staticmethod
    def reminderWithEventStore_(store):
        return FakeReminder()


# The apple_reminders backend reaches real pyobjc bridges (Foundation's
# NSDateComponents/NSDate) for date conversion, which the EventKit doubles do
# not stand in for. pyobjc is macOS-only, so on Linux CI these three raise
# ModuleNotFoundError: No module named 'Foundation'. Skip there rather than
# fake Foundation — the backend only ever runs on the Mac that owns the
# Reminders database.
_needs_pyobjc = pytest.mark.skipif(
    importlib.util.find_spec("Foundation") is None,
    reason="pyobjc (Foundation) is macOS-only; apple_reminders never runs on Linux",
)


@pytest.fixture
def ar(monkeypatch):
    from src.brain.plugins.tasks import apple_reminders as mod

    store = FakeStore(calendars=[FakeCalendar("Inbox"), FakeCalendar("Career")])
    monkeypatch.setattr(mod, "eventkit_store", lambda: store)
    monkeypatch.setattr(mod, "_reminder_class", lambda: FakeReminderClass)
    return mod.AppleRemindersTaskManager(), store


def test_backends_satisfy_protocol() -> None:
    from src.brain.plugins.tasks.apple_reminders import AppleRemindersTaskManager
    from src.brain.plugins.tasks.noop import NoopTaskManager
    from src.brain.plugins.tasks.todoist import TodoistTaskManager

    for cls in (NoopTaskManager, TodoistTaskManager, AppleRemindersTaskManager):
        assert isinstance(cls(), TaskManager), cls.__name__


def test_loader_resolves_apple_reminders() -> None:
    from src.brain.plugins.loader import load_tasks
    from src.brain.plugins.tasks.apple_reminders import AppleRemindersTaskManager

    tm = load_tasks({"tasks": "apple_reminders"})
    assert isinstance(tm, AppleRemindersTaskManager)


@_needs_pyobjc
def test_add_creates_reminder_in_matched_list(ar) -> None:
    mgr, store = ar
    out = asyncio.run(
        mgr.add(
            "Submit visa form",
            description="bring passport",
            project="career",
            priority=4,
            due="2026-07-10",
            labels=["family"],
        )
    )
    assert out["id"].startswith("FAKE-")
    assert out["url"] is None
    (rem,) = store.saved
    assert rem.title() == "Submit visa form"
    assert rem.calendar().title() == "Career"
    assert rem.priority() == 1
    assert "bring passport" in rem.notes()
    assert "family" in rem.notes()
    assert rem.dueDateComponents() is not None


def test_add_save_failure_raises(ar) -> None:
    mgr, store = ar
    # Partial failure: saved=True but err is set — BOTH must be checked.
    store.save_result = (True, "NSError boom")
    with pytest.raises(RuntimeError):
        asyncio.run(mgr.add("X"))


def test_add_unknown_list_raises(ar) -> None:
    mgr, _ = ar
    with pytest.raises(RuntimeError):
        asyncio.run(mgr.add("X", project="nonexistent"))


@_needs_pyobjc
def test_list_pending_normalizes_shape(ar) -> None:
    mgr, _ = ar
    asyncio.run(mgr.add("Buy milk", project="inbox", priority=3, due="2026-07-09"))
    items = asyncio.run(mgr.list_pending())
    assert len(items) == 1
    t = items[0]
    assert t["content"] == "Buy milk"
    assert t["title"] == "Buy milk"
    assert t["project_id"] == "Inbox"
    assert t["priority"] == 3
    assert t["due"] == {"date": "2026-07-09"}
    assert t["id"].startswith("FAKE-")


@_needs_pyobjc
def test_list_completed_returns_completed_only(ar) -> None:
    mgr, store = ar
    a = asyncio.run(mgr.add("Done thing"))
    asyncio.run(mgr.add("Open thing"))
    store.calendarItemWithIdentifier_(a["id"]).setCompleted_(True)
    done = asyncio.run(mgr.list_completed(since_hours=24, limit=10))
    assert [t["content"] for t in done] == ["Done thing"]
    assert done[0]["completed_at"]
    pending = asyncio.run(mgr.list_pending())
    assert [t["content"] for t in pending] == ["Open thing"]


def test_dedup_and_add_fuzzy_matches_pending(isolated_db) -> None:
    from src.brain.plugins.tasks._dedup import dedup_and_add

    fake = AsyncMock()
    fake.list_pending = AsyncMock(
        return_value=[{"content": "Submit OpenAI Codex application", "id": "1"}]
    )
    out = asyncio.run(dedup_and_add(fake, "Apply to OpenAI Codex"))
    assert out["deduped"] is True
    fake.add.assert_not_awaited()


def test_dedup_and_add_creates_and_records_hash(isolated_db) -> None:
    from src.brain.plugins.tasks._dedup import check_local_hash, dedup_and_add, title_hash
    from src.db import get_db

    fake = AsyncMock()
    fake.list_pending = AsyncMock(return_value=[])
    fake.list_completed = AsyncMock(return_value=[])
    fake.add = AsyncMock(return_value={"id": "ABC-123", "url": None})

    out = asyncio.run(dedup_and_add(fake, "Brand new errand", project="Inbox"))
    assert out["deduped"] is False
    fake.add.assert_awaited_once()
    prior = check_local_hash(get_db(), title_hash("Brand new errand"))
    assert prior is not None
    assert prior["todoist_id"] == "ABC-123"


def test_tasks_list_renders_both_backend_shapes() -> None:
    from src.brain.mcp.tools import tasks as tools_mod

    fake = AsyncMock()
    fake.list_pending = AsyncMock(
        return_value=[
            {"id": "6gPh3f", "content": "Pay rent", "project_id": "123",
             "priority": 3, "due": {"date": "2026-07-08"}},
            {"id": "AAAA-BBBB", "title": "Buy milk", "content": "Buy milk",
             "project_id": "Inbox", "priority": 1, "due": None},
        ]
    )
    with patch.object(tools_mod, "tasks", return_value=fake):
        out = asyncio.run(tools_mod.tasks_list({}))
    text = out["content"][0]["text"]
    assert "Pay rent" in text
    assert "Buy milk" in text
    assert "due=[2026-07-08]" in text
