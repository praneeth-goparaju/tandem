from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _iso_utc(minutes_ago: float) -> str:
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%S")


def test_slack_listener_skips_when_key_absent(isolated_db):
    from src.brain.doctor import checks

    assert checks.check_slack_listener(None, Path(".")) == []


def test_slack_listener_missing_after_startup_grace_warns(isolated_db):
    from src.brain.doctor import checks
    from src.brain.state import set_state

    set_state("daemon_started_at", _iso_utc(30))
    issues = checks.check_slack_listener(None, Path("."))
    assert len(issues) == 1
    assert "never reported healthy" in issues[0].message


def test_slack_listener_fresh_is_green(isolated_db):
    from src.brain.doctor import checks
    from src.brain.state import set_state

    set_state("slack_listener_heartbeat", _iso_utc(2))
    assert checks.check_slack_listener(None, Path(".")) == []


def test_slack_listener_stale_warns(isolated_db):
    from src.brain.doctor import checks
    from src.brain.state import set_state

    set_state("slack_listener_heartbeat", _iso_utc(30))
    issues = checks.check_slack_listener(None, Path("."))
    assert len(issues) == 1
    assert "listener" in issues[0].message.lower()


def test_quick_check_ok_on_healthy_db(isolated_db):
    from src.brain.doctor import checks

    isolated_db.get_db()
    assert checks.check_memory_db_integrity(None, Path(".")) == []


def test_quick_check_skips_when_db_missing(isolated_db, monkeypatch, tmp_path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.setattr(config, "MEMORY_DB_PATH", tmp_path / "nope.db")
    assert checks.check_memory_db_integrity(None, Path(".")) == []


def test_quick_check_flags_garbage_file(isolated_db, monkeypatch, tmp_path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    garbage = tmp_path / "corrupt.db"
    garbage.write_bytes(b"this is not a sqlite database at all" * 100)
    monkeypatch.setattr(config, "MEMORY_DB_PATH", garbage)
    issues = checks.check_memory_db_integrity(None, Path("."))
    assert len(issues) == 1


def test_backup_freshness_is_opt_in(isolated_db, monkeypatch):
    from src.brain.doctor import checks

    monkeypatch.delenv("BRAIN_BACKUP_EXPECTED", raising=False)
    assert checks.check_backup_freshness(None, Path(".")) == []


def test_backup_freshness_missing_stamp_warns(isolated_db, monkeypatch):
    from src.brain.doctor import checks

    monkeypatch.setenv("BRAIN_BACKUP_EXPECTED", "1")
    issues = checks.check_backup_freshness(None, Path("."))
    assert len(issues) == 1
    assert "missing" in issues[0].message


def test_backup_freshness_accepts_recent_private_stamp(isolated_db, monkeypatch):
    from src.brain.doctor import checks
    from src.orchestrator import config

    stamp = Path(config.MEMORY_DB_PATH).parent / "backup-success.stamp"
    stamp.write_text(_iso_utc(60) + "Z\n", encoding="utf-8")
    stamp.chmod(0o600)
    monkeypatch.setenv("BRAIN_BACKUP_EXPECTED", "1")

    assert checks.check_backup_freshness(None, Path(".")) == []


def test_backup_freshness_stale_stamp_warns(isolated_db, monkeypatch):
    from src.brain.doctor import checks
    from src.orchestrator import config

    stamp = Path(config.MEMORY_DB_PATH).parent / "backup-success.stamp"
    stamp.write_text(_iso_utc(37 * 60) + "Z\n", encoding="utf-8")
    stamp.chmod(0o600)
    monkeypatch.setenv("BRAIN_BACKUP_EXPECTED", "1")

    issues = checks.check_backup_freshness(None, Path("."))
    assert len(issues) == 1
    assert "stale" in issues[0].message


def test_backup_freshness_rejects_unsafe_or_malformed_stamp(
    isolated_db, monkeypatch
):
    from src.brain.doctor import checks
    from src.orchestrator import config

    stamp = Path(config.MEMORY_DB_PATH).parent / "backup-success.stamp"
    stamp.write_text("SECRET-SHOULD-NOT-BE-ECHOED\n", encoding="utf-8")
    stamp.chmod(0o644)
    monkeypatch.setenv("BRAIN_BACKUP_EXPECTED", "1")

    issues = checks.check_backup_freshness(None, Path("."))
    assert "mode 0600" in issues[0].message
    stamp.chmod(0o600)
    issues = checks.check_backup_freshness(None, Path("."))
    assert "valid timestamp" in issues[0].message
    assert "SECRET-SHOULD-NOT-BE-ECHOED" not in issues[0].message


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return {"status": "sent", "id": "1712345678.000123"}


def _results(with_issue: bool):
    from src.brain.doctor import Check, Issue

    def _noop(cfg, root):
        return []

    check = Check(name="check_example", severity="warn", fn=_noop)
    issues = [Issue(file="daemon_state", message="something is stale")] if with_issue else []
    return [(check, issues)]


def test_ping_skips_when_channel_unset(monkeypatch):
    from src.brain.doctor.notify import send_failure_ping
    from src.orchestrator import config

    monkeypatch.setattr(config, "BRIEF_CHANNEL_ID", "")
    fake = FakeNotifier()
    assert asyncio.run(send_failure_ping(_results(True), notifier=fake)) is False
    assert fake.sent == []


def test_ping_skips_when_all_green(monkeypatch):
    from src.brain.doctor.notify import send_failure_ping
    from src.orchestrator import config

    monkeypatch.setattr(config, "BRIEF_CHANNEL_ID", "C0123456789")
    fake = FakeNotifier()
    assert asyncio.run(send_failure_ping(_results(False), notifier=fake)) is False
    assert fake.sent == []


def test_ping_sends_when_channel_set_and_check_fails(monkeypatch):
    from src.brain.doctor.notify import send_failure_ping
    from src.orchestrator import config

    monkeypatch.setattr(config, "BRIEF_CHANNEL_ID", "C0123456789")
    fake = FakeNotifier()
    assert asyncio.run(send_failure_ping(_results(True), notifier=fake)) is True
    assert len(fake.sent) == 1
    text, kwargs = fake.sent[0]
    assert "check_example" in text
    assert "something is stale" in text


def test_the_garmin_lag_limit_allows_a_day_plus_the_jobs_misfire_grace():
    from src.brain.doctor.checks import _GARMIN_LAG_LIMIT_MIN
    from src.orchestrator.scheduler import GARMIN_MISFIRE_GRACE_SECS

    assert _GARMIN_LAG_LIMIT_MIN * 60 > 24 * 3600 + GARMIN_MISFIRE_GRACE_SECS
