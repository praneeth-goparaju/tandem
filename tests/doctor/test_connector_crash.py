from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from src.brain.doctor import CHECKS, Check, Issue, checks, print_report, run_checks

CONNECTOR_CHECKS = (
    "check_connector_config",
    "check_connector_sign_in",
    "check_connector_instructions",
    "check_connector_integrations",
    "check_connector_daily_note_template",
)


@pytest.fixture(autouse=True)
def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "overlay"
    folder.mkdir()
    monkeypatch.setenv("USER_DIR", str(folder))
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    return folder


def _write_config(overlay: Path, tmp_path: Path, data_dir: str) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (overlay / "vault_connector.yaml").write_text(
        f"vault_path: {vault}\ndata_dir: {data_dir}\npublic_url: https://vault.example.com\n",
        encoding="utf-8",
    )


def _only(monkeypatch: pytest.MonkeyPatch, *selected: Check) -> None:
    monkeypatch.setattr("src.brain.doctor.CHECKS", list(selected))
    monkeypatch.setattr("src.brain.runtime.loader.load_all", lambda root: None)


def _registered(name: str) -> Check:
    return next(check for check in CHECKS if check.name == name)


@pytest.mark.parametrize("name", CONNECTOR_CHECKS)
def test_a_crashing_connector_check_becomes_a_warning(
    name: str, overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(overlay, tmp_path, str(tmp_path / "data"))

    def boom(*args, **kwargs):
        raise RuntimeError("the connector library is missing")

    monkeypatch.setattr(checks, "_connector_config", boom)
    issues = _registered(name).fn(None, Path("."))
    assert len(issues) == 1
    assert issues[0].severity == "warn"
    assert issues[0].message.startswith(f"{name} crashed: RuntimeError: the connector library")


def test_a_crashing_connector_check_does_not_stop_the_daemon(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _write_config(overlay, tmp_path, str(tmp_path / "data"))

    def boom(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'pyotp'")

    monkeypatch.setattr(checks, "_connector_config", boom)
    _only(monkeypatch, *(_registered(name) for name in CONNECTOR_CHECKS))
    results, code = run_checks(Path("."), strict=False)
    assert code == 0
    print_report(results)
    out = capsys.readouterr().out
    assert "PASS with 5 warning(s)" in out
    assert "FAIL" not in out


def test_warn_checks_count_as_errors_only_under_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    warn = Check(name="check_w", severity="warn", fn=lambda cfg, root: [Issue("f", "careful")])
    info = Check(
        name="check_i", severity="warn", fn=lambda cfg, root: [Issue("f", "fyi", severity="info")]
    )
    _only(monkeypatch, warn, info)
    assert run_checks(Path("."), strict=False)[1] == 0
    assert run_checks(Path("."), strict=True)[1] == 1
    _only(monkeypatch, info)
    assert run_checks(Path("."), strict=True)[1] == 0


def test_report_and_exit_code_agree_on_a_crashed_check(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def crash(cfg, root):
        raise ValueError("bad")

    _only(monkeypatch, Check(name="check_crash", severity="warn", fn=crash))
    results, code = run_checks(Path("."), strict=False)
    print_report(results)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL — 1 error(s), 0 warning(s)" in out
    assert "✗ check_crash (error)" in out


def test_sign_in_check_needs_none_of_the_connector_libraries(
    overlay: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(overlay, tmp_path, str(tmp_path / "data"))
    for name in ("pyotp", "argon2", "argon2.exceptions"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "src.brain.connector.auth.provider", raising=False)
    issues = checks.check_connector_sign_in(None, Path("."))
    assert len(issues) == 1
    assert issues[0].severity is None
    assert "set-password" in issues[0].message
    assert "src.brain.connector.auth.provider" not in sys.modules


def test_a_relative_data_dir_with_an_auth_db_does_not_crash(
    overlay: Path, tmp_path: Path
) -> None:
    _write_config(overlay, tmp_path, "connector-data")
    data = overlay / "connector-data"
    data.mkdir(mode=0o700)
    with sqlite3.connect(data / "auth.db") as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('password_hash', 'h')")
        conn.execute("INSERT INTO settings VALUES ('totp_secret', 't')")
    assert checks.check_connector_sign_in(None, Path(".")) == []
