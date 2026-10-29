from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.orchestrator import config


def _modes() -> dict[str, SimpleNamespace]:
    return {
        "all_morning": SimpleNamespace(schedule="0 0 * * *", misfire_grace_seconds=None),
        "manual_only": SimpleNamespace(schedule=None, misfire_grace_seconds=None),
    }


def _capture_spawns(monkeypatch) -> list[str]:
    from src.orchestrator import scheduler

    spawned: list[str] = []

    def fake_spawn(coro, label, _logger):
        spawned.append(label)
        coro.close()

    monkeypatch.setattr(scheduler, "spawn_tracked", fake_spawn)
    return spawned


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_agents_paused_reads_truthy_values(monkeypatch, value) -> None:
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", value)
    assert config.agents_paused() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "paused"])
def test_agents_paused_is_off_for_other_values(monkeypatch, value) -> None:
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", value)
    assert config.agents_paused() is False


def test_agents_paused_is_off_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("BRAIN_AGENTS_PAUSED", raising=False)
    assert config.agents_paused() is False


def test_modes_are_scheduled_when_not_paused(monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    instance = scheduler.create_scheduler()

    assert instance.get_job("all_morning") is not None
    assert instance.get_job("manual_only") is None


def test_no_mode_is_scheduled_when_paused(monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    instance = scheduler.create_scheduler()

    assert instance.get_job("all_morning") is None


def test_code_jobs_stay_scheduled_when_paused(monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    monkeypatch.setattr(config, "GARMIN_ENABLED", True)
    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    instance = scheduler.create_scheduler()

    assert instance.get_job("garmin_sync") is not None
    assert instance.get_job("garmin_sync_intraday") is None
    garmin = instance.get_job("garmin_sync")
    assert (str(garmin.trigger.fields[5]), str(garmin.trigger.fields[6])) == ("7", "0")
    assert garmin.kwargs == {"days_back": 7, "include_today": True}


def test_morning_catch_up_fires_when_not_paused(isolated_db, monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    spawned = _capture_spawns(monkeypatch)

    asyncio.run(scheduler._maybe_catch_up_morning())

    assert spawned == ["morning catch-up"]


def test_morning_catch_up_does_nothing_when_paused(isolated_db, monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    spawned = _capture_spawns(monkeypatch)

    asyncio.run(scheduler._maybe_catch_up_morning())

    assert spawned == []
    row = isolated_db.get_db().execute(
        "SELECT value FROM daemon_state WHERE key='last_morning_trigger'"
    ).fetchone()
    assert row is None


def test_the_local_backup_runs_nightly_even_when_agents_are_paused(monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    backup = scheduler.create_scheduler().get_job("local_backup")

    assert backup is not None
    assert (str(backup.trigger.fields[5]), str(backup.trigger.fields[6])) == ("23", "35")


def test_smart_reminders_are_checked_every_five_minutes(monkeypatch) -> None:
    from src.orchestrator import registry, scheduler

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.setattr(registry, "MODE_SPECS", _modes())
    job = scheduler.create_scheduler().get_job("smart_reminders")

    assert job is not None and str(job.trigger.fields[6]) == "*/5"
