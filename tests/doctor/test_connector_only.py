from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.brain.doctor import CHECKS, print_report, run_checks
from src.brain.doctor import checks as _checks  # noqa: F401  (registers the checks)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_ONLY = {
    "check_private_configuration_permissions",
    "check_connector_config",
    "check_connector_sign_in",
    "check_connector_instructions",
    "check_connector_integrations",
    "check_connector_daily_note_template",
}


def _private(path: Path) -> None:
    for current, _dirs, files in os.walk(path):
        os.chmod(current, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


@pytest.fixture
def stranger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A connector-only install: profile, connector settings, sign-in, a vault."""
    overlay = tmp_path / "user"
    overlay.mkdir(mode=0o700)
    vault = tmp_path / "vault"
    (vault / "11 - Templates").mkdir(parents=True)
    (vault / "11 - Templates" / "Daily Note.md").write_text("# {{date}}\n", encoding="utf-8")
    data = tmp_path / "data"
    (overlay / "profile.yaml").write_text("user:\n  timezone: America/New_York\n", encoding="utf-8")
    (overlay / "vault_connector.yaml").write_text(
        f"vault_path: {vault}\ndata_dir: {data}\npublic_url: https://vault.example.com\n",
        encoding="utf-8",
    )
    data.mkdir(mode=0o700)
    with sqlite3.connect(data / "auth.db") as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('password_hash', 'h')")
        conn.execute("INSERT INTO settings VALUES ('totp_secret', 't')")
    _private(overlay)
    monkeypatch.setenv("USER_DIR", str(overlay))
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    return tmp_path


def test_connector_only_runs_the_connector_checks_and_passes(stranger: Path, capsys) -> None:
    results, code = run_checks(REPO_ROOT, connector_only=True)
    assert {check.name for check, _ in results} == CONNECTOR_ONLY
    print_report(results)
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out and "FAIL" not in out and "warning" not in out


def test_the_cli_flag_runs_only_the_connector_checks(stranger: Path) -> None:
    env = {**os.environ, "HOME": str(stranger / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "doctor", "--connector"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "check_vault_runtime_path" not in result.stdout
    assert "check_connector_sign_in" in result.stdout


def test_connector_only_fails_without_the_connector_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    overlay = tmp_path / "user"
    overlay.mkdir(mode=0o700)
    monkeypatch.setenv("USER_DIR", str(overlay))
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    results, code = run_checks(REPO_ROOT, connector_only=True)
    print_report(results)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "vault_connector.yaml" in out


def test_connector_only_fails_on_a_connector_warning(stranger: Path, capsys) -> None:
    config = stranger / "user" / "vault_connector.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("https://vault.example.com", "http://vault.example.com"),
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(stranger / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "doctor", "--connector"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "https://" in result.stdout
