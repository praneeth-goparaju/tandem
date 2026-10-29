from __future__ import annotations

import asyncio

from src.brain.actions.handlers import complete_reminder


class Reminder:
    def __init__(self, *, completed: bool = False) -> None:
        self.completed = completed

    def title(self):
        return "Call school"

    def isCompleted(self):
        return self.completed

    def setCompleted_(self, value):
        self.completed = bool(value)


class Store:
    def __init__(self, reminder=None, *, verify: bool = True) -> None:
        self.reminder = reminder
        self.verify = verify
        self.saved = False

    def calendarItemWithIdentifier_(self, _identifier):
        if self.saved and not self.verify:
            return None
        return self.reminder

    def saveReminder_commit_error_(self, reminder, *_args):
        self.reminder = reminder
        self.saved = True
        return True, None


def test_missing_id_fails_before_eventkit() -> None:
    result = asyncio.run(complete_reminder.execute({}))
    assert result["ok"] is False
    assert "missing reminder id" in result["result"]


def test_unknown_id_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store", lambda: Store(),
    )
    result = asyncio.run(complete_reminder.execute({"id": "missing"}))
    assert result["ok"] is False
    assert "not found" in result["result"]


def test_marks_reminder_complete_and_verifies(monkeypatch) -> None:
    reminder = Reminder()
    store = Store(reminder)
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store", lambda: store,
    )

    result = asyncio.run(complete_reminder.execute({"id": "ABC-123"}))

    assert result["ok"] is True
    assert reminder.completed is True
    assert store.saved is True
    assert "completed reminder" in result["result"]


def test_already_complete_is_idempotent(monkeypatch) -> None:
    reminder = Reminder(completed=True)
    store = Store(reminder)
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store", lambda: store,
    )

    result = asyncio.run(complete_reminder.execute({"id": "ABC-123"}))

    assert result["ok"] is True
    assert result["already_complete"] is True
    assert store.saved is False


def test_unverified_save_is_partial_success(monkeypatch) -> None:
    store = Store(Reminder(), verify=False)
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store", lambda: store,
    )

    result = asyncio.run(complete_reminder.execute({"id": "ABC-123"}))

    assert result["ok"] is True
    assert result["partial"] is True
    assert "could not verify" in result["result"]


def test_save_exception_is_ambiguous(monkeypatch) -> None:
    class FailingStore(Store):
        def saveReminder_commit_error_(self, reminder, *_args):
            raise TimeoutError("response lost")

    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.eventkit_store",
        lambda: FailingStore(Reminder()),
    )

    result = asyncio.run(complete_reminder.execute({"id": "ABC-123"}))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True
