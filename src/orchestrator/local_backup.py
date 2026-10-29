"""Nightly local backup run by the daemon: the vault's history and a copy of the database.

The vault is committed into a separate Git directory (`BRAIN_VAULT_HISTORY`,
default `~/brain-vault.git`) whose work tree is the vault, so the vault itself
holds no `.git`. The database is copied with SQLite's online backup API into
`<database folder>/backups/`, keeping the newest `KEEP_DATABASE_COPIES`. Both
stay on this Mac. After both succeed, the success stamp that
`check_backup_freshness` reads is advanced. Running inside the daemon matters:
macOS lets the daemon read the iCloud vault, which a separate launchd shell
script was refused.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from src.brain.atomic_io import atomic_write_text

log = logging.getLogger("brain.local_backup")

KEEP_DATABASE_COPIES = 7
_GIT_TIMEOUT_SECONDS = 600


class BackupError(RuntimeError):
    """A backup step failed; the success stamp was not advanced."""


def vault_history_dir() -> Path:
    return Path(os.getenv("BRAIN_VAULT_HISTORY", "~/brain-vault.git")).expanduser()


def stamp_path(database: Path) -> Path:
    override = os.getenv("BRAIN_BACKUP_STAMP_PATH", "").strip()
    return Path(override).expanduser() if override else database.parent / "backup-success.stamp"


def _git(history: Path, vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git", path=os.environ.get("PATH", "/usr/bin:/bin"))
    if git is None:
        raise BackupError("git was not found on PATH")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "GIT_DIR": str(history),
        "GIT_WORK_TREE": str(vault),
    }
    return subprocess.run(  # noqa: S603
        [git, *args], cwd=vault, env=env, capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_SECONDS, check=False,
    )


def snapshot_vault(vault: Path, history: Path, *, today: str) -> str:
    """Commit every change in the vault; say whether anything was new."""
    if not history.is_dir():
        raise BackupError(f"vault history {history} is missing; create it with git init --bare")
    added = _git(history, vault, "add", "-A")
    if added.returncode != 0:
        raise BackupError(f"git add failed: {(added.stderr or added.stdout).strip()[:500]}")
    staged = _git(history, vault, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return "vault unchanged"
    if staged.returncode != 1:
        raise BackupError(f"git diff failed: {staged.stderr.strip()[:500]}")
    committed = _git(
        history, vault, "-c", "user.name=tandem", "-c", "user.email=tandem@localhost",
        "commit", "-q", "-m", f"auto: nightly backup {today}",
    )
    if committed.returncode != 0:
        raise BackupError(f"git commit failed: {(committed.stderr or committed.stdout).strip()[:500]}")
    return "vault committed"


def copy_database(database: Path, *, today: str) -> Path:
    """Copy the live database with SQLite's backup API; keep the newest few copies."""
    folder = database.parent / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / f"{database.stem}-{today}.db"
    partial = target.with_suffix(".db.partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    os.chmod(partial, 0o600)
    os.replace(partial, target)
    copies = sorted(folder.glob(f"{database.stem}-*.db"))
    for old in copies[:-KEEP_DATABASE_COPIES]:
        old.unlink()
    return target


def run_backup(*, vault: Path, database: Path, history: Path | None = None) -> str:
    """Back up the vault and the database; advance the success stamp only when both worked."""
    today = datetime.now().strftime("%Y-%m-%d")
    vault_result = snapshot_vault(vault, history or vault_history_dir(), today=today)
    try:
        copy = copy_database(database, today=today)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"database copy failed: {exc}") from exc
    atomic_write_text(
        stamp_path(database), datetime.now(UTC).isoformat(timespec="seconds") + "\n",
        mode=0o600, preserve_existing_mode=False,
    )
    return f"{vault_result}; database copied to {copy.name}"


def main() -> None:
    from src.orchestrator.config import MEMORY_DB_PATH, VAULT_PATH

    print(run_backup(vault=Path(VAULT_PATH), database=Path(MEMORY_DB_PATH)))


if __name__ == "__main__":
    main()
