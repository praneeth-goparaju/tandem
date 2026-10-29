from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return path


def _environment(tmp_path: Path, *, check_result: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sqlite = _executable(
        fake_bin / "sqlite3",
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  '.backup '*) : > \"${2#'.backup '}\" ;;\n"
        f"  *) echo {check_result!r} ;;\n"
        "esac\n",
    )
    restic_log = tmp_path / "restic.log"
    _executable(
        fake_bin / "restic",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$RESTIC_TEST_LOG\"\n"
        "if [ -n \"${RESTIC_FAIL_ON:-}\" ]; then\n"
        "  case \"$*\" in *\"$RESTIC_FAIL_ON\"*) exit 9 ;; esac\n"
        "fi\n"
        "exit 0\n",
    )
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o700)
    memory_db = state / "vault.db"
    memory_db.touch()
    memory_db.chmod(0o600)
    housing_db = state / "housing.db"
    housing_db.touch()
    housing_db.chmod(0o600)
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    user = tmp_path / "user"
    user.mkdir(mode=0o700)
    password = tmp_path / "restic-password"
    password.write_text("synthetic-test-password", encoding="utf-8")
    password.chmod(0o600)
    env = {
        **os.environ,
        "BRAIN_PYTHON": sys.executable,
        "MEMORY_DB_PATH": str(memory_db),
        "VAULT_PATH": str(vault),
        "USER_DIR": str(user),
        "BRAIN_BACKUP_STAMP_PATH": str(state / "backup-success.stamp"),
        "BRAIN_BACKUP_ALERT": "0",
        "RESTIC_REPOSITORY": "synthetic:test",
        "RESTIC_PASSWORD_FILE": str(password),
        "RESTIC_TEST_LOG": str(restic_log),
        "SQLITE3_BIN": str(sqlite),
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
    }
    return env, restic_log


def test_backup_refuses_corrupt_snapshot_before_upload(tmp_path: Path) -> None:
    env, log = _environment(tmp_path, check_result="corrupt")
    result = subprocess.run(
        ["bash", "scripts/backup_memory.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing upload" in result.stderr
    assert " backup " not in f" {log.read_text(encoding='utf-8')} "


def test_backup_snapshots_memory_and_housing_databases(tmp_path: Path) -> None:
    env, log = _environment(tmp_path, check_result="ok")
    result = subprocess.run(
        ["bash", "scripts/backup_memory.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    backup = next(line for line in commands.splitlines() if " backup " in f" {line} ")
    assert "memory-backup.db" in backup
    assert "housing-backup.db" in backup
    stamp = Path(env["BRAIN_BACKUP_STAMP_PATH"])
    assert stamp.read_text(encoding="utf-8").endswith("Z\n")
    assert stamp.stat().st_mode & 0o777 == 0o600


def test_backup_requires_configured_vault_and_user_roots(tmp_path: Path) -> None:
    env, log = _environment(tmp_path, check_result="ok")
    env["VAULT_PATH"] = str(tmp_path / "missing-vault")

    result = subprocess.run(
        ["bash", "scripts/backup_memory.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "vault directory must be a real directory" in result.stderr
    assert not log.exists()
    assert not Path(env["BRAIN_BACKUP_STAMP_PATH"]).exists()


def test_backup_allows_explicit_root_opt_out(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path, check_result="ok")
    env["VAULT_PATH"] = str(tmp_path / "missing-vault")
    env["USER_DIR"] = str(tmp_path / "missing-user")
    env["BRAIN_BACKUP_SKIP_VAULT"] = "1"
    env["BRAIN_BACKUP_SKIP_USER"] = "1"

    result = subprocess.run(
        ["bash", "scripts/backup_memory.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_backup_rejects_symlink_and_permissive_roots(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path, check_result="ok")
    actual_vault = Path(env["VAULT_PATH"])
    link = tmp_path / "vault-link"
    link.symlink_to(actual_vault, target_is_directory=True)
    env["VAULT_PATH"] = str(link)

    linked = subprocess.run(
        ["bash", "scripts/backup_memory.sh"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    assert linked.returncode != 0
    assert "not a symlink" in linked.stderr

    env["VAULT_PATH"] = str(actual_vault)
    actual_vault.chmod(0o755)
    permissive = subprocess.run(
        ["bash", "scripts/backup_memory.sh"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    assert permissive.returncode != 0
    assert "mode 0700" in permissive.stderr


def test_backup_failure_does_not_advance_success_stamp(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path, check_result="ok")
    env["RESTIC_FAIL_ON"] = "forget"
    stamp = Path(env["BRAIN_BACKUP_STAMP_PATH"])
    stamp.write_text("2020-01-01T00:00:00Z\n", encoding="utf-8")
    stamp.chmod(0o600)

    result = subprocess.run(
        ["bash", "scripts/backup_memory.sh"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert stamp.read_text(encoding="utf-8") == "2020-01-01T00:00:00Z\n"
