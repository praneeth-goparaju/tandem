from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.brain.doctor import CHECKS, Issue, checks


@pytest.fixture(autouse=True)
def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "overlay"
    folder.mkdir()
    monkeypatch.setenv("USER_DIR", str(folder))
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    for name in ("ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD"):
        monkeypatch.setenv(name, "")
    return folder


def write_config(overlay: Path, tmp_path: Path, **overrides: str) -> dict[str, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    data = tmp_path / "data"
    values = {
        "vault_path": str(vault),
        "data_dir": str(data),
        "public_url": "https://vault.example.com",
    }
    values.update(overrides)
    text = "".join(f"{key}: {value}\n" for key, value in values.items())
    (overlay / "vault_connector.yaml").write_text(text, encoding="utf-8")
    return {"vault": vault, "data": data}


def messages(issues: list[Issue]) -> str:
    return " | ".join(issue.message for issue in issues)


def test_missing_connector_config_is_only_info(overlay: Path) -> None:
    issues = checks.check_connector_config(None, Path("."))
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert "examples/vault_connector.example.yaml" in issues[0].message
    assert checks.check_connector_sign_in(None, Path(".")) == []
    assert checks.check_connector_instructions(None, Path(".")) == []
    assert checks.check_connector_integrations(None, Path(".")) == []


def test_missing_connector_config_is_quiet_when_runtime_checks_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")
    assert checks.check_connector_config(None, Path(".")) == []


def test_good_connector_config_is_clean(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path)
    assert checks.check_connector_config(None, Path(".")) == []


def test_missing_vault_errors(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, vault_path=str(tmp_path / "nowhere"))
    issues = checks.check_connector_config(None, Path("."))
    assert [issue.severity for issue in issues] == [None]
    assert "vault_path" in issues[0].message


def test_data_dir_inside_vault_errors(overlay: Path, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write_config(overlay, tmp_path, data_dir=str(vault / "state"))
    issues = checks.check_connector_config(None, Path("."))
    assert [issue.severity for issue in issues] == [None]
    assert "data_dir" in issues[0].message and "inside" in issues[0].message


def test_vault_inside_data_dir_errors(overlay: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    vault = data / "vault"
    vault.mkdir(parents=True)
    write_config(overlay, tmp_path, vault_path=str(vault), data_dir=str(data))
    issues = checks.check_connector_config(None, Path("."))
    assert [issue.severity for issue in issues] == [None]
    assert "vault_path" in issues[0].message and "inside" in issues[0].message


def test_http_public_url_errors(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, public_url="http://vault.example.com")
    issues = checks.check_connector_config(None, Path("."))
    assert [issue.severity for issue in issues] == [None]
    assert "https://" in issues[0].message


def test_unreadable_config_reports_the_error_text(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, port="99999")
    issues = checks.check_connector_config(None, Path("."))
    assert [issue.severity for issue in issues] == [None]
    assert "port must be a number from 1 to 65535" in issues[0].message


def test_sign_in_not_set_errors(overlay: Path, tmp_path: Path) -> None:
    paths = write_config(overlay, tmp_path)
    issues = checks.check_connector_sign_in(None, Path("."))
    assert len(issues) == 1
    assert "set-password" in issues[0].message and "totp-setup" in issues[0].message
    assert not (paths["data"] / "auth.db").exists()


def test_sign_in_set_is_clean_and_prints_no_secret(overlay: Path, tmp_path: Path) -> None:
    paths = write_config(overlay, tmp_path)
    paths["data"].mkdir(mode=0o700)
    db = paths["data"] / "auth.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('password_hash', 'hash-secret')")
        conn.execute("INSERT INTO settings VALUES ('totp_secret', 'totp-secret')")
    assert checks.check_connector_sign_in(None, Path(".")) == []


def test_sign_in_with_only_a_password_names_the_missing_step(
    overlay: Path, tmp_path: Path
) -> None:
    paths = write_config(overlay, tmp_path)
    paths["data"].mkdir(mode=0o700)
    with sqlite3.connect(paths["data"] / "auth.db") as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('password_hash', 'hash-secret')")
    issues = checks.check_connector_sign_in(None, Path("."))
    assert len(issues) == 1
    assert "totp-setup" in issues[0].message
    assert "hash-secret" not in issues[0].message


def test_instructions_too_long_errors(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(overlay, tmp_path)
    (overlay / "connector_instructions.yaml").write_text(
        "extra_rules:\n" + "".join(f"  - rule number {n}\n" for n in range(400)),
        encoding="utf-8",
    )
    issues = checks.check_connector_instructions(None, Path("."))
    assert len(issues) == 1
    assert "lines long" in issues[0].message


def test_instructions_with_a_bad_overlay_errors(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path)
    (overlay / "connector_instructions.yaml").write_text("nonsense: 1\n", encoding="utf-8")
    issues = checks.check_connector_instructions(None, Path("."))
    assert len(issues) == 1
    assert "nonsense" in issues[0].message


def test_instructions_with_a_bad_profile_routing_row_errors(
    overlay: Path, tmp_path: Path
) -> None:
    write_config(overlay, tmp_path)
    (overlay / "profile.yaml").write_text(
        "reminders:\n  routing:\n    - {who: Child A}\n", encoding="utf-8"
    )
    issues = checks.check_connector_instructions(None, Path("."))
    assert len(issues) == 1
    assert "reminders.routing[0].list" in issues[0].message


def test_instructions_render_cleanly(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path)
    assert checks.check_connector_instructions(None, Path(".")) == []


def test_integrations_summary_info(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path)
    issues = checks.check_connector_integrations(None, Path("."))
    assert [issue.severity for issue in issues] == ["info"]
    assert "none" in issues[0].message


def test_integrations_summary_names_the_enabled_ones(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(overlay, tmp_path, integrations="{calendar: caldav, mail: imap}")
    monkeypatch.setenv("ICLOUD_APPLE_ID", "someone@example.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "app-secret")
    issues = checks.check_connector_integrations(None, Path("."))
    assert [issue.severity for issue in issues] == ["info"]
    assert "calendar" in issues[0].message and "mail" in issues[0].message


def test_integration_without_credentials_errors(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, integrations="{contacts: carddav}")
    issues = checks.check_connector_integrations(None, Path("."))
    assert sorted(str(issue.severity) for issue in issues) == ["None", "info"]
    error = next(issue for issue in issues if issue.severity is None)
    assert "contacts" in error.message and "ICLOUD_APP_PASSWORD" in error.message


def _never_prompt():
    raise AssertionError("the doctor must not ask macOS for Reminders access")


def test_reminders_without_access_here_is_info_and_never_prompts(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(overlay, tmp_path, integrations="{reminders: eventkit}")
    monkeypatch.setattr(checks.sys, "platform", "darwin")
    monkeypatch.setattr("src.brain.mcp.tools._icloud.eventkit_store", _never_prompt)
    monkeypatch.setattr("src.brain.mcp.tools._icloud.reminders_access_granted", lambda: False)
    issues = checks.check_connector_integrations(None, Path("."))
    assert [issue.severity for issue in issues] == ["info", "info"]
    assert "Reminders access" in issues[1].message


def test_reminders_with_access_adds_nothing(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(overlay, tmp_path, integrations="{reminders: eventkit}")
    monkeypatch.setattr(checks.sys, "platform", "darwin")
    monkeypatch.setattr("src.brain.mcp.tools._icloud.eventkit_store", _never_prompt)
    monkeypatch.setattr("src.brain.mcp.tools._icloud.reminders_access_granted", lambda: True)
    issues = checks.check_connector_integrations(None, Path("."))
    assert [issue.severity for issue in issues] == ["info"]


def test_connector_checks_are_warnings_so_they_never_block_the_daemon() -> None:
    names = {
        "check_connector_config",
        "check_connector_sign_in",
        "check_connector_instructions",
        "check_connector_integrations",
        "check_connector_daily_note_template",
    }
    found = {check.name: check.severity for check in CHECKS if check.name in names}
    assert found == dict.fromkeys(names, "warn")


def test_a_bad_config_file_is_reported_once(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, port="99999")
    all_issues = [
        *checks.check_connector_config(None, Path(".")),
        *checks.check_connector_sign_in(None, Path(".")),
        *checks.check_connector_instructions(None, Path(".")),
        *checks.check_connector_integrations(None, Path(".")),
    ]
    assert len(all_issues) == 1
    assert "port" in all_issues[0].message


def test_a_locked_auth_db_is_not_reported_as_not_set_up(overlay: Path, tmp_path: Path) -> None:
    paths = write_config(overlay, tmp_path)
    paths["data"].mkdir(mode=0o700)
    db = paths["data"] / "auth.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('password_hash', 'hash-secret')")
        conn.execute("INSERT INTO settings VALUES ('totp_secret', 'totp-secret')")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        issues = checks.check_connector_sign_in(None, Path("."))
    finally:
        holder.close()
    assert len(issues) == 1
    assert "cannot read" in issues[0].message
    assert "set-password" not in issues[0].message
    assert "secret" not in issues[0].message


def test_a_corrupt_auth_db_is_not_reported_as_not_set_up(overlay: Path, tmp_path: Path) -> None:
    paths = write_config(overlay, tmp_path)
    paths["data"].mkdir(mode=0o700)
    (paths["data"] / "auth.db").write_bytes(b"this is not a database" * 100)
    issues = checks.check_connector_sign_in(None, Path("."))
    assert len(issues) == 1
    assert "cannot read" in issues[0].message
    assert "set-password" not in issues[0].message


def test_the_summary_names_the_zone_and_where_it_came_from(overlay: Path, tmp_path: Path) -> None:
    (overlay / "profile.yaml").write_text("user:\n  timezone: America/New_York\n", encoding="utf-8")
    write_config(overlay, tmp_path)
    issues = checks.check_connector_integrations(None, Path("."))
    assert "time zone America/New_York (from profile.yaml)" in issues[0].message


def test_a_missing_daily_note_template_warns(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path)
    issues = checks.check_connector_daily_note_template(None, Path("."))
    assert len(issues) == 1
    assert issues[0].severity is None
    assert "11 - Templates/Daily Note.md" in issues[0].message
    assert "init-vault" in issues[0].message


def test_the_daily_note_template_follows_the_profile(overlay: Path, tmp_path: Path) -> None:
    (overlay / "profile.yaml").write_text(
        "notes:\n  templates_dir: Tpl\n  daily_note_template: Day.md\n", encoding="utf-8"
    )
    paths = write_config(overlay, tmp_path)
    (paths["vault"] / "Tpl").mkdir()
    (paths["vault"] / "Tpl" / "Day.md").write_text("x\n", encoding="utf-8")
    assert checks.check_connector_daily_note_template(None, Path(".")) == []


def test_no_template_warning_without_a_vault(overlay: Path, tmp_path: Path) -> None:
    write_config(overlay, tmp_path, vault_path=str(tmp_path / "nowhere"))
    assert checks.check_connector_daily_note_template(None, Path(".")) == []
