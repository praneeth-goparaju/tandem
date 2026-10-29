from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _routine(*action_types: str):
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="routine",
        area="assistant",
        allowed_action_types=frozenset(action_types),
        untrusted_input=True,
    )


def _scheduled(*action_types: str):
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="health",
        area="health",
        allowed_action_types=frozenset(action_types),
    )


def _propose(caller, **kwargs):
    from src.brain.mcp.caller_context import caller_scope
    from src.brain.mcp.tools.actions import propose_action

    handler = getattr(propose_action, "handler", propose_action)
    with caller_scope(caller):
        return asyncio.run(handler(kwargs))


def _stub_execution(monkeypatch) -> None:
    from src.brain.actions import queue

    async def _fake_run_one(row):
        queue.transition_status(row["id"], "approved", "executed", result="ok")

    monkeypatch.setattr("src.brain.actions.executor._run_one", _fake_run_one)


_EVENT_URL = "https://p01-caldav.icloud.com/123/calendars/home/event.ics"


def _attendees(monkeypatch, value: bool) -> None:
    async def involves_others(event_url):
        assert event_url == _EVENT_URL
        return value

    monkeypatch.setattr(
        "src.brain.actions.handlers.update_calendar_event.event_involves_others", involves_others,
    )


_SOLO_EVENT = {
    "summary": "Focus block",
    "start": "2026-10-02T09:00:00+02:00",
    "end": "2026-10-02T10:00:00+02:00",
}


@pytest.mark.parametrize(
    "type_, payload",
    [
        ("create_reminder", {"title": "Pack the documents", "due": "2026-10-02T18:00:00+02:00"}),
        ("create_todoist_task", {"content": "Claim the cloud credit", "project_id": "Inbox"}),
        ("create_calendar_event", dict(_SOLO_EVENT)),
        ("complete_reminder", {"id": "reminder-42"}),
        ("update_calendar_event", {"event_url": _EVENT_URL, "start": "2026-10-02T10:00:00+02:00"}),
    ],
)
def test_untrusted_routine_own_device_action_runs_without_approval(
    isolated_db, monkeypatch, type_, payload
) -> None:
    from src.brain.actions import queue

    _stub_execution(monkeypatch)
    _attendees(monkeypatch, False)

    out = _propose(_routine(type_), type=type_, payload=payload, rationale="from today's mail")

    assert "executed" in out["content"][0]["text"].lower()
    assert queue.list_by_status("pending") == []
    executed = queue.list_by_status("executed")
    assert len(executed) == 1
    assert executed[0]["type"] == type_
    assert executed[0]["risk"] == "auto"


def test_untrusted_routine_calendar_event_with_attendees_still_needs_approval(
    isolated_db,
) -> None:
    from src.brain.actions import queue

    with patch("src.brain.mcp.tools.actions.notifier") as review_notifier:
        review_notifier.return_value.send = AsyncMock(
            return_value={"status": "sent", "id": "1.1"}
        )
        out = _propose(
            _routine("create_calendar_event"),
            type="create_calendar_event",
            payload={**_SOLO_EVENT, "attendees": ["someone@example.com"]},
            rationale="invite from a mail thread",
        )

    assert "risk=confirm" in out["content"][0]["text"]
    pending = queue.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["risk"] == "confirm"


def test_an_update_to_an_event_with_attendees_still_needs_approval(isolated_db, monkeypatch) -> None:
    from src.brain.actions import queue

    _attendees(monkeypatch, True)
    with patch("src.brain.mcp.tools.actions.notifier") as review_notifier:
        review_notifier.return_value.send = AsyncMock(return_value={"status": "sent", "id": "1.1"})
        out = _propose(
            _scheduled("update_calendar_event"),
            type="update_calendar_event",
            payload={"event_url": _EVENT_URL, "start": "2026-10-02T10:00:00+02:00"},
            rationale="moved by the owner",
        )

    assert "risk=confirm" in out["content"][0]["text"]
    assert queue.list_by_status("pending")[0]["risk"] == "confirm"


def test_an_event_that_cannot_be_checked_counts_as_involving_others(monkeypatch) -> None:
    from src.brain.actions.handlers import update_calendar_event

    def unreachable(event_url):
        raise OSError("no network")

    monkeypatch.setattr(update_calendar_event, "_event_involves_others_sync", unreachable)
    assert asyncio.run(update_calendar_event.event_involves_others(_EVENT_URL)) is True


def test_scheduled_caller_explicit_confirm_on_reminder_is_ignored(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue

    _stub_execution(monkeypatch)

    out = _propose(
        _scheduled("create_reminder"),
        type="create_reminder",
        payload={"title": "Take a 45-minute walk", "due": "2026-10-02T17:30:00+02:00"},
        rationale="today's step floor",
        risk="confirm",
    )

    assert "executed" in out["content"][0]["text"].lower()
    assert queue.list_by_status("pending") == []
    assert queue.list_by_status("executed")[0]["risk"] == "auto"


def test_explicit_confirm_on_other_types_is_still_honored(isolated_db) -> None:
    from src.brain.actions import queue

    out = _propose(
        _scheduled("push_notification"),
        type="push_notification",
        payload={"title": "Heads up", "message": "Your parcel arrives today"},
        rationale="worth a look",
        risk="confirm",
    )

    assert "risk=confirm" in out["content"][0]["text"]
    assert queue.list_by_status("pending")[0]["risk"] == "confirm"
