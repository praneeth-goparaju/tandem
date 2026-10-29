from __future__ import annotations

import asyncio
from pathlib import Path


def test_action_executor_starts_without_reply_listener(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from src.brain.actions import executor
    from src.brain.mcp import _common
    from src.brain.plugins import loader
    from src.orchestrator import scheduler

    started: list[str] = []

    async def fake_executor() -> None:
        return None

    def fake_spawn(coro, label, _logger):
        started.append(label)
        coro.close()
        return None

    monkeypatch.setattr(executor, "run_forever", fake_executor)
    monkeypatch.setattr(_common, "integrations_config", lambda: {})
    monkeypatch.setattr(loader, "load_listener", lambda _cfg: None)
    monkeypatch.setattr(scheduler, "spawn_tracked", fake_spawn)

    asyncio.run(scheduler._start_listeners())

    assert started == ["action executor"]


def test_reply_listener_starts_alongside_executor(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from src.brain.actions import executor
    from src.brain.mcp import _common
    from src.brain.plugins import loader
    from src.orchestrator import reply_routing, scheduler

    started: list[str] = []

    async def fake_executor() -> None:
        return None

    class FakeListener:
        async def run(self, callback) -> None:
            assert callback is reply_routing.on_reply

    def fake_spawn(coro, label, _logger):
        started.append(label)
        coro.close()
        return None

    monkeypatch.setattr(executor, "run_forever", fake_executor)
    monkeypatch.setattr(_common, "integrations_config", lambda: {})
    monkeypatch.setattr(loader, "load_listener", lambda _cfg: FakeListener())
    monkeypatch.setattr(scheduler, "spawn_tracked", fake_spawn)

    asyncio.run(scheduler._start_listeners())

    assert started == ["action executor", "reply listener"]


def test_no_scheduler_job_drains_a_remote_intake(monkeypatch) -> None:
    from src.orchestrator import config, scheduler

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    instance = scheduler.create_scheduler()

    assert instance.get_job("intake_sync") is None


def test_backup_monitor_is_scheduled_only_when_expected(monkeypatch) -> None:
    from src.orchestrator import config, scheduler

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.delenv("BRAIN_BACKUP_EXPECTED", raising=False)
    assert scheduler.create_scheduler().get_job("backup_freshness") is None

    monkeypatch.setenv("BRAIN_BACKUP_EXPECTED", "1")
    job = scheduler.create_scheduler().get_job("backup_freshness")
    assert job is not None
    assert str(job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='9', minute='15']"
