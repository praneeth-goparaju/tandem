from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.brain.actions.handlers import create_reminder
from src.brain.connector import notify as notify_module
from src.brain.connector.notify import alert_time, notify

BERLIN = ZoneInfo("Europe/Berlin")


def test_daytime_alert_one_minute_ahead() -> None:
    now = datetime(2026, 10, 22, 14, 0, tzinfo=BERLIN)

    result = alert_time(now, "Europe/Berlin")

    assert result == datetime(2026, 10, 22, 14, 1, tzinfo=BERLIN)
    assert result.tzinfo is not None


def test_naive_now_is_local_time() -> None:
    result = alert_time(datetime(2026, 10, 22, 14, 0), "Europe/Berlin")

    assert result.isoformat() == "2026-10-22T14:01:00+02:00"


def test_aware_now_in_another_zone_is_converted() -> None:
    result = alert_time(datetime(2026, 10, 22, 12, 0, tzinfo=UTC), "Europe/Berlin")

    assert result.isoformat() == "2026-10-22T14:01:00+02:00"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 22, 22, 15, tzinfo=BERLIN), "2026-10-23T06:00:00+02:00"),
        (datetime(2026, 10, 22, 5, 59, tzinfo=BERLIN), "2026-10-22T06:00:00+02:00"),
        (datetime(2026, 10, 22, 21, 29, tzinfo=BERLIN), "2026-10-23T06:00:00+02:00"),
        (datetime(2026, 10, 22, 21, 28, tzinfo=BERLIN), "2026-10-22T21:29:00+02:00"),
        (datetime(2026, 10, 22, 23, 0, tzinfo=BERLIN), "2026-10-23T06:00:00+02:00"),
        (datetime(2026, 10, 22, 6, 0, tzinfo=BERLIN), "2026-10-22T06:01:00+02:00"),
    ],
)
def test_quiet_hours_moved_to_0600(now: datetime, expected: str) -> None:
    assert alert_time(now, "Europe/Berlin").isoformat() == expected


def test_dst_night_still_0600() -> None:
    now = datetime(2026, 10, 24, 23, 30, tzinfo=BERLIN)

    result = alert_time(now, "Europe/Berlin")

    assert result.isoformat() == "2026-10-25T06:00:00+01:00"
    assert result.astimezone(UTC) == datetime(2026, 10, 25, 5, 0, tzinfo=UTC)

    during_repeated_hour = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN)
    shifted = alert_time(during_repeated_hour, "Europe/Berlin")
    assert shifted.isoformat() == "2026-10-25T06:00:00+01:00"
    assert shifted.astimezone(UTC) == datetime(2026, 10, 25, 5, 0, tzinfo=UTC)


def test_dst_spring_forward_night_still_0600() -> None:
    result = alert_time(datetime(2026, 3, 28, 23, 0, tzinfo=BERLIN), "Europe/Berlin")

    assert result.isoformat() == "2026-03-29T06:00:00+02:00"
    assert result.astimezone(UTC) == datetime(2026, 3, 29, 4, 0, tzinfo=UTC)


def test_notify_builds_payload_and_calls_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)
    now = datetime(2026, 10, 22, 14, 0, tzinfo=BERLIN)

    result = notify(
        "Vault connector is down", "Family", notes="no answer", now=now, timezone="Europe/Berlin"
    )

    assert result == {"ok": True, "result": "created"}
    assert len(seen) == 1
    expected = alert_time(now, "Europe/Berlin").isoformat()
    assert seen[0] == {
        "title": "Vault connector is down",
        "notes": "no answer",
        "list_name": "Family",
        "due": expected,
        "alarm": expected,
    }
    assert expected == "2026-10-22T14:01:00+02:00"


def test_notify_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_execute(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("EventKit exploded")

    monkeypatch.setattr(create_reminder, "execute", failing_execute)

    result = notify("Title", "Family", notes="n")

    assert result["ok"] is False
    assert "EventKit exploded" in result["result"]


def test_notify_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def slow_execute(payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True, "result": "late"}

    monkeypatch.setattr(create_reminder, "execute", slow_execute)
    monkeypatch.setattr(notify_module, "EVENTKIT_TIMEOUT_SECONDS", 0.05)

    assert notify("Title", "Family") == {"ok": False, "result": "timed out"}


def test_notify_works_from_threads_without_a_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "result": payload["title"]}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: notify(f"t{n}", "Family"), range(8)))

    assert [r["result"] for r in results] == [f"t{n}" for n in range(8)]


def test_notify_logs_only_start_of_notes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing_execute(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("no")

    monkeypatch.setattr(create_reminder, "execute", failing_execute)
    notes = "a" * 80 + "SECRET-TAIL"

    with caplog.at_level("DEBUG"):
        notify("Title", "Family", notes=notes)

    assert "SECRET-TAIL" not in caplog.text


def test_alert_time_one_minute_is_a_timedelta_of_sixty_seconds() -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=BERLIN)

    assert alert_time(now, "Europe/Berlin") - now == timedelta(minutes=1)


def test_notify_off_logs_and_returns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    called: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        called.append(payload)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)

    with caplog.at_level("WARNING", logger=notify_module.__name__):
        result = notify("Vault connector is down", "Family", notes="no answer", enabled=False)

    assert result == {"ok": False, "result": "reminders integration is off"}
    assert called == []
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert caplog.records[0].getMessage() == (
        "reminders integration is off, alert not sent: Vault connector is down"
    )


def test_quiet_hours_follow_the_given_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)
    late_in_new_york = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)

    notify("Title", "Family", now=late_in_new_york, timezone="America/New_York")

    assert seen[0]["due"] == "2026-10-23T06:00:00-04:00"


def test_without_a_zone_quiet_hours_follow_the_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)
    monkeypatch.setenv("TZ", "America/New_York")

    notify("Title", "Family", now=datetime(2026, 10, 23, 2, 0, tzinfo=UTC))

    assert seen[0]["due"] == "2026-10-23T06:00:00-04:00"
