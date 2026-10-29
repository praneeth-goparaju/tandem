from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from src.brain.runtime.loader import resolve_user_dir

BACKUP_FOLDER = "vault-edit-backups"
KEEP_DAYS = 30
_REPO_ROOT = Path(__file__).resolve().parents[3]


def backup_root() -> Path:
    return resolve_user_dir(_REPO_ROOT) / BACKUP_FOLDER


def _private_folders(folder: Path) -> None:
    """Create `folder` and any missing parents below USER_DIR, each owner-only."""
    root = backup_root().parent
    missing = []
    current = folder
    while not current.exists() and current != root:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)


def keep_copy(target: Path, relative: str, *, now: datetime | None = None) -> Path | None:
    """Copy a vault file before a direct edit changes it; None when there is no file yet.

    The copy goes to `<USER_DIR>/vault-edit-backups/<date>/<vault path>.<time>`,
    readable by the owner only.
    """
    if not target.is_file():
        return None
    moment = now or datetime.now()
    destination = backup_root() / moment.strftime("%Y-%m-%d") / f"{relative}.{moment:%H%M%S%f}"
    _private_folders(destination.parent)
    shutil.copy2(target, destination)
    os.chmod(destination, 0o600)
    return destination


def delete_old(keep_days: int = KEEP_DAYS, *, today: date | None = None) -> int:
    """Delete the day folders older than `keep_days`; return how many went."""
    root = backup_root()
    if not root.is_dir():
        return 0
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    deleted = 0
    for folder in root.iterdir():
        try:
            day = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if day < cutoff and folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
            deleted += 1
    return deleted
