import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.orchestrator import local_backup


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_BACKUP_STAMP_PATH", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("first\n")
    history = tmp_path / "history.git"
    subprocess.run(["git", "init", "-q", "--bare", str(history)], check=True)
    data = tmp_path / "data"
    data.mkdir()
    database = data / "vault.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    return vault, history, database


def commits(history: Path) -> int:
    out = subprocess.run(["git", "--git-dir", str(history), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=False)
    return int(out.stdout.strip() or 0)


def test_a_backup_commits_the_vault_copies_the_database_and_stamps(setup):
    vault, history, database = setup
    result = local_backup.run_backup(vault=vault, database=database, history=history)

    assert result.startswith("vault committed")
    assert commits(history) == 1
    [copy] = (database.parent / "backups").glob("vault-*.db")
    assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchall() == [(1,)]
    assert copy.stat().st_mode & 0o777 == 0o600
    stamp = database.parent / "backup-success.stamp"
    assert stamp.stat().st_mode & 0o777 == 0o600 and stamp.read_text().strip().endswith("+00:00")


def test_an_unchanged_vault_makes_no_new_commit(setup):
    vault, history, database = setup
    local_backup.run_backup(vault=vault, database=database, history=history)
    assert local_backup.run_backup(vault=vault, database=database, history=history).startswith("vault unchanged")
    assert commits(history) == 1


def test_a_failed_vault_snapshot_does_not_advance_the_stamp(setup):
    vault, _history, database = setup
    with pytest.raises(local_backup.BackupError, match="missing"):
        local_backup.run_backup(vault=vault, database=database, history=vault.parent / "nowhere.git")
    assert not (database.parent / "backup-success.stamp").exists()


def test_a_git_error_is_reported_not_read_as_no_changes(setup, monkeypatch):
    vault, history, database = setup
    real = local_backup._git

    def refusing(history_dir, vault_dir, *args):
        if args[:1] == ("add",):
            return subprocess.CompletedProcess(args, 128, "", "error: open(\".x\"): Operation not permitted")
        return real(history_dir, vault_dir, *args)

    monkeypatch.setattr(local_backup, "_git", refusing)
    with pytest.raises(local_backup.BackupError, match="Operation not permitted"):
        local_backup.run_backup(vault=vault, database=database, history=history)


def test_only_the_newest_seven_database_copies_are_kept(setup):
    _vault, _history, database = setup
    for day in range(1, 10):
        local_backup.copy_database(database, today=f"2026-10-0{day}")
    kept = sorted(p.name for p in (database.parent / "backups").glob("vault-*.db"))
    assert kept == [f"vault-2026-10-0{day}.db" for day in range(3, 10)]
