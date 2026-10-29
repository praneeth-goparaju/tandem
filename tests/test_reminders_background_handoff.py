"""Reminder actions proposed from a process macOS refuses Reminders access to.

A routine runs inside the Claude desktop app. macOS holds the desktop app
responsible for the gateway's one-shot bridge process, and the desktop app can
never be granted Reminders access, so a reminder handler run in the bridge
always fails. The background service is its own responsible process and holds
the grant. propose_action therefore approves a reminder action and leaves it
for the background service's executor instead of running the handler itself.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from src.brain.mcp.tools._icloud import (
    reminders_access_granted as _real_reminders_access_granted,
)

_REMINDER = {"title": "Pack the documents", "due": "2026-10-04T18:00:00+02:00"}


def _routine(*action_types: str):
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="routine",
        area="assistant",
        allowed_action_types=frozenset(action_types),
        untrusted_input=True,
    )


def _trusted_client(*action_types: str):
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="external-mcp",
        area="assistant",
        allowed_action_types=frozenset(action_types),
        direct_action_types=frozenset(action_types),
    )


def _propose(caller, **kwargs) -> str:
    from src.brain.mcp.caller_context import caller_scope
    from src.brain.mcp.tools.actions import propose_action

    handler = getattr(propose_action, "handler", propose_action)
    with caller_scope(caller):
        return asyncio.run(handler(kwargs))["content"][0]["text"]


def _reminders_access(monkeypatch, granted: bool) -> None:
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.reminders_access_granted", lambda: granted
    )


class _Processes:
    def __init__(self, monkeypatch, *action_types: str, service_running: bool = True):
        from src.brain.actions import executor
        from src.brain.actions.types import ActionType

        self.ran_in: list[str] = []
        self._in_service = False

        async def _handler(_payload):
            self.ran_in.append("service" if self._in_service else "local")
            return {"ok": True, "result": "done by the stub handler"}

        for action_type in action_types:
            monkeypatch.setitem(executor.HANDLERS, ActionType(action_type), _handler)

        real_sleep = asyncio.sleep

        async def _sleep(seconds):
            if service_running:
                self._in_service = True
                try:
                    await executor.drain_once()
                finally:
                    self._in_service = False
            await real_sleep(min(seconds, 0.01))

        monkeypatch.setattr("src.brain.mcp.tools.actions.asyncio.sleep", _sleep)


def test_routine_reminder_without_local_access_is_executed_by_the_background_service(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue

    _reminders_access(monkeypatch, granted=False)
    processes = _Processes(monkeypatch, "create_reminder")

    text = _propose(
        _routine("create_reminder"),
        type="create_reminder",
        payload=dict(_REMINDER),
        rationale="from today's mail",
    )

    assert processes.ran_in == ["service"]
    assert "executed: done by the stub handler" in text
    executed = queue.list_by_status("executed")
    assert len(executed) == 1
    assert executed[0]["approval_channel"] == "auto"


def test_trusted_client_complete_reminder_keeps_its_approval_record_when_handed_over(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue

    _reminders_access(monkeypatch, granted=False)
    processes = _Processes(monkeypatch, "complete_reminder")

    text = _propose(
        _trusted_client("complete_reminder"),
        type="complete_reminder",
        payload={"id": "reminder-42"},
        rationale="done this morning",
    )

    assert processes.ran_in == ["service"]
    assert "executed: done by the stub handler" in text
    executed = queue.list_by_status("executed")[0]
    assert executed["approval_channel"] == "trusted_client"
    assert executed["approval_actor"] == "external-mcp"


def test_reminder_stays_approved_and_is_reported_as_not_run_when_the_service_is_down(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools import actions

    _reminders_access(monkeypatch, granted=False)
    processes = _Processes(monkeypatch, "create_reminder", service_running=False)
    monkeypatch.setattr(actions, "_SERVICE_WAIT_SECS", 0.05)
    monkeypatch.setattr(actions, "_SERVICE_POLL_SECS", 0.01)

    text = _propose(
        _routine("create_reminder"),
        type="create_reminder",
        payload=dict(_REMINDER),
        rationale="from today's mail",
    )

    assert processes.ran_in == []
    approved = queue.list_by_status("approved")
    assert len(approved) == 1
    assert approved[0]["id"] in text
    assert "has not run yet" in text
    assert "executed" not in text.lower()


def test_reminder_runs_in_this_process_when_it_has_reminders_access(
    isolated_db, monkeypatch
) -> None:
    _reminders_access(monkeypatch, granted=True)
    processes = _Processes(monkeypatch, "create_reminder")

    text = _propose(
        _routine("create_reminder"),
        type="create_reminder",
        payload=dict(_REMINDER),
        rationale="from today's mail",
    )

    assert processes.ran_in == ["local"]
    assert "executed: done by the stub handler" in text


def test_calendar_event_still_runs_in_this_process_without_reminders_access(
    isolated_db, monkeypatch
) -> None:
    _reminders_access(monkeypatch, granted=False)
    processes = _Processes(monkeypatch, "create_calendar_event")

    text = _propose(
        _routine("create_calendar_event"),
        type="create_calendar_event",
        payload={
            "summary": "Focus block",
            "start": "2026-10-04T09:00:00+02:00",
            "end": "2026-10-04T10:00:00+02:00",
        },
        rationale="from today's plan",
    )

    assert processes.ran_in == ["local"]
    assert "executed: done by the stub handler" in text


@pytest.mark.parametrize(
    "status, granted",
    [(0, False), (1, False), (2, False), (3, True)],
    ids=["not-determined", "restricted", "denied", "full-access"],
)
def test_reminders_access_granted_reads_the_status_without_asking_macos(
    monkeypatch, status, granted
) -> None:
    asked: list[int] = []

    class _Store:
        @staticmethod
        def authorizationStatusForEntityType_(entity_type):  # noqa: N802
            asked.append(entity_type)
            return status

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "EventKit", types.SimpleNamespace(
        EKEventStore=_Store, EKEntityTypeReminder=1, EKAuthorizationStatusFullAccess=3,
    ))

    assert _real_reminders_access_granted() is granted
    assert asked == [1]  # 1 = EKEntityTypeReminder


def test_reminders_access_is_not_granted_where_eventkit_does_not_exist(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert _real_reminders_access_granted() is False
