from __future__ import annotations

import os
import stat
from pathlib import Path

from src.brain.atomic_io import ensure_real_directory


def sqlite_files(path: Path) -> tuple[Path, ...]:
    path = Path(path)
    return (
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
        path.with_name(path.name + "-journal"),
    )


def secure_existing_sqlite_file(path: Path) -> None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise RuntimeError(f"database path is not a regular file: {path}")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise PermissionError(f"database file is not owned by this user: {path}")
    os.chmod(path, 0o600, follow_symlinks=False)


def secure_sqlite_path(path: Path) -> Path:
    path = Path(path).expanduser()
    parent = ensure_real_directory(path.parent)
    value = parent.lstat()
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise PermissionError(f"database parent is not owned by this user: {parent}")
    os.chmod(parent, 0o700, follow_symlinks=False)
    path = parent / path.name
    for candidate in sqlite_files(path):
        secure_existing_sqlite_file(candidate)
    return path


def secure_sqlite_files(path: Path) -> None:
    for candidate in sqlite_files(path):
        secure_existing_sqlite_file(candidate)
