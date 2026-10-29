from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scripts import connector_health
from scripts.connector_health import run_check
from src.brain.actions.handlers import create_reminder
from src.brain.connector.config import AlertLists, ConnectorConfig, Integrations
from src.brain.connector.notify import notify

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def config(tmp_path: Path) -> ConnectorConfig:
    return ConnectorConfig(
        vault_path=tmp_path / "Vault",
        data_dir=tmp_path,
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
        alert_lists=AlertLists(household="Family"),
        integrations=Integrations(reminders=True),
    )


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.enabled: list[bool] = []
        self.zones: list[str | None] = []

    def __call__(
        self,
        title: str,
        list_name: str,
        notes: str = "",
        now: datetime | None = None,
        *,
        enabled: bool = True,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((title, list_name, notes))
        self.enabled.append(enabled)
        self.zones.append(timezone)
        return {"ok": True, "result": "created"}


def _down(url: str) -> tuple[int, str]:
    return 503, "unavailable"


def _up(url: str) -> tuple[int, str]:
    return 200, "ok"


def _state(config: ConnectorConfig) -> dict[str, Any]:
    return json.loads((config.data_dir / "health.json").read_text(encoding="utf-8"))


def test_health_alerts_after_two_failures_once(config: ConnectorConfig) -> None:
    notify_fn = Recorder()

    run_check(config, _down, notify_fn, NOW)
    assert notify_fn.calls == []
    assert _state(config) == {"failures": 1, "alerted": False}

    run_check(config, _down, notify_fn, NOW)
    assert len(notify_fn.calls) == 1
    title, list_name, notes = notify_fn.calls[0]
    assert title == "Vault connector is down"
    assert list_name == "Family"
    assert "503" in notes
    assert _state(config) == {"failures": 2, "alerted": True}
    assert notify_fn.enabled == [True]

    run_check(config, _down, notify_fn, NOW)
    assert len(notify_fn.calls) == 1
    assert _state(config) == {"failures": 3, "alerted": True}


def test_health_resets_after_success(config: ConnectorConfig) -> None:
    notify_fn = Recorder()
    for _ in range(2):
        run_check(config, _down, notify_fn, NOW)
    assert len(notify_fn.calls) == 1

    run_check(config, _up, notify_fn, NOW)
    assert _state(config) == {"failures": 0, "alerted": False}

    run_check(config, _down, notify_fn, NOW)
    assert len(notify_fn.calls) == 1
    run_check(config, _down, notify_fn, NOW)
    assert len(notify_fn.calls) == 2


def test_failed_alert_is_retried_next_run(config: ConnectorConfig) -> None:
    calls: list[str] = []

    def failing_notify(
        title: str,
        list_name: str,
        notes: str = "",
        now: datetime | None = None,
        *,
        enabled: bool = True,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        calls.append(title)
        return {"ok": False, "result": "no such list"}

    for _ in range(3):
        run_check(config, _down, failing_notify, NOW)

    assert len(calls) == 2
    assert _state(config)["alerted"] is False


def test_exception_from_fetch_counts_as_failure(config: ConnectorConfig) -> None:
    notify_fn = Recorder()

    def broken(url: str) -> tuple[int, str]:
        raise ConnectionError("connection refused")

    run_check(config, broken, notify_fn, NOW)
    run_check(config, broken, notify_fn, NOW)

    assert len(notify_fn.calls) == 1
    assert "connection refused" in notify_fn.calls[0][2]


def test_wrong_body_or_status_is_a_failure(config: ConnectorConfig) -> None:
    notify_fn = Recorder()

    run_check(config, lambda url: (200, "<html>login</html>"), notify_fn, NOW)
    run_check(config, lambda url: (302, "ok"), notify_fn, NOW)

    assert len(notify_fn.calls) == 1


def test_fetch_gets_the_health_url(config: ConnectorConfig) -> None:
    urls: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        urls.append(url)
        return 200, "ok"

    run_check(config, fetch, Recorder(), NOW)

    assert urls == ["https://vault.example.com/health"]


def test_state_file_is_private_and_atomic(config: ConnectorConfig) -> None:
    run_check(config, _down, Recorder(), NOW)

    path = config.data_dir / "health.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in config.data_dir.iterdir()] == ["health.json"]


def test_damaged_state_file_starts_fresh(config: ConnectorConfig) -> None:
    (config.data_dir / "health.json").write_text("{not json", encoding="utf-8")

    run_check(config, _down, Recorder(), NOW)

    assert _state(config) == {"failures": 1, "alerted": False}


def test_main_exits_2_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> ConnectorConfig:
        raise ValueError("vault_connector.yaml: public_url is required")

    monkeypatch.setattr(connector_health, "load_config", broken)

    assert connector_health.main() == 2
    assert "public_url is required" in capsys.readouterr().err


def test_health_alert_with_reminders_off_does_not_crash(
    config: ConnectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    called: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        called.append(payload)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(create_reminder, "execute", fake_execute)
    off = replace(config, integrations=Integrations())

    with caplog.at_level("WARNING"):
        states = [run_check(off, _down, notify, NOW) for _ in range(3)]

    assert called == []
    assert states == [
        {"failures": 1, "alerted": False},
        {"failures": 2, "alerted": False},
        {"failures": 3, "alerted": False},
    ]
    assert _state(off) == {"failures": 3, "alerted": False}
    assert caplog.text.count("alert not sent: Vault connector is down") == 2


def test_the_alert_uses_the_connector_zone(tmp_path: Path) -> None:
    config = ConnectorConfig(
        vault_path=tmp_path / "Vault",
        data_dir=tmp_path,
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
        integrations=Integrations(reminders=True),
        timezone="America/New_York",
    )
    notify_fn = Recorder()
    run_check(config, _down, notify_fn, NOW)
    run_check(config, _down, notify_fn, NOW)
    assert notify_fn.zones == ["America/New_York"]


def test_main_exits_0_when_the_connector_is_down(
    config: ConnectorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_answer(url: str) -> tuple[int, str]:
        raise OSError("connection refused")

    monkeypatch.setattr(connector_health, "load_config", lambda: config)
    monkeypatch.setattr(connector_health, "fetch_health", no_answer)
    monkeypatch.setattr(connector_health, "notify", Recorder())

    assert connector_health.main() == 0
    assert connector_health.main() == 0
    assert _state(config) == {"failures": 2, "alerted": True}
