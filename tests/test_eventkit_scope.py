from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


def _cfg(*, tasks="noop", actions=()):
    return SimpleNamespace(
        integrations={"tasks": tasks},
        agents={"agent": SimpleNamespace(allowed_action_types=frozenset(actions))},
    )


def test_doctor_skips_eventkit_when_no_reminder_surface(monkeypatch) -> None:
    from src.brain.doctor import checks
    from src.brain.mcp.tools import _icloud

    called = False

    def fake_store():
        nonlocal called
        called = True

    monkeypatch.setattr(checks.sys, "platform", "darwin")
    monkeypatch.setattr(_icloud, "eventkit_store", fake_store)

    assert checks.check_icloud_eventkit_access(_cfg(), Path(".")) == []
    assert called is False


def test_doctor_checks_explicit_apple_reminders_backend(monkeypatch) -> None:
    from src.brain.doctor import checks
    from src.brain.mcp.tools import _icloud

    called = False

    def fake_store():
        nonlocal called
        called = True

    monkeypatch.setattr(checks.sys, "platform", "darwin")
    monkeypatch.setattr(_icloud, "eventkit_store", fake_store)

    assert checks.check_icloud_eventkit_access(
        _cfg(tasks="apple_reminders"), Path(".")
    ) == []
    assert called is True


def test_doctor_rejects_apple_reminders_on_non_macos(monkeypatch) -> None:
    from src.brain.doctor import checks

    monkeypatch.setattr(checks.sys, "platform", "linux")
    issues = checks.check_icloud_eventkit_access(
        _cfg(tasks="apple_reminders"), Path(".")
    )
    assert len(issues) == 1
    assert "requires macOS" in issues[0].message


def test_eventkit_store_requests_reminders_only(monkeypatch) -> None:
    from src.brain.mcp.tools import _icloud

    calls: list[str] = []
    fake_store = object()
    monkeypatch.setattr(_icloud, "_eventkit_store", None)
    monkeypatch.setattr(_icloud, "_wait_for_reminders_access", lambda store: calls.append("reminders"))

    class Factory:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return fake_store

    monkeypatch.setitem(sys.modules, "EventKit", SimpleNamespace(EKEventStore=Factory))

    assert _icloud.eventkit_store() is fake_store
    assert calls == ["reminders"]
