"""Copy the shipped vault templates into a folder, never overwriting."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from src.brain.atomic_io import atomic_create_text
from src.brain.connector.layout import VaultLayout

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates" / "vault"
FILE_MODE = 0o644
DIR_MODE = 0o755

_TEMPLATE_INBOX = "00 - Inbox"
_TEMPLATE_DAILY_NOTES = "01 - Daily Notes"
_TEMPLATE_TEMPLATES = "11 - Templates"
_TEMPLATE_ACTIONS = "Actions"
_TEMPLATE_DAILY_NOTE = "Daily Note.md"


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _destination(relative: PurePosixPath, layout: VaultLayout) -> PurePosixPath:
    folders = {
        _TEMPLATE_INBOX: layout.inbox_dir,
        _TEMPLATE_DAILY_NOTES: layout.daily_notes_dir,
        _TEMPLATE_TEMPLATES: layout.templates_dir,
        _TEMPLATE_ACTIONS: layout.actions_dir,
    }
    first, rest = relative.parts[0], relative.parts[1:]
    if first not in folders or not rest:
        return relative
    if first == _TEMPLATE_TEMPLATES and rest == (_TEMPLATE_DAILY_NOTE,):
        rest = (layout.daily_note_template,)
    return PurePosixPath(folders[first], *rest)


def _make_folder(path: Path) -> None:
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"{path} exists and is not a folder") from None
        return
    os.chmod(path, DIR_MODE)


def _make_folders(target: Path, relative: PurePosixPath) -> None:
    current = target
    for part in relative.parts:
        current = current / part
        _make_folder(current)


def _make_target(target: Path) -> None:
    missing: list[Path] = []
    current = target
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        _make_folder(folder)
    if not target.is_dir():
        raise ValueError(f"{target} is not a folder")


def _refuse_repo(target: Path) -> None:
    """Refuse ``target`` when it or any existing folder above it is the repository."""
    repo = os.stat(REPO_ROOT)
    for folder in (target, *target.parents):
        try:
            info = os.stat(folder)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (repo.st_dev, repo.st_ino):
            raise ValueError(f"refusing to write a vault inside the repository: {target}")


def _template_files(templates: Path) -> list[PurePosixPath]:
    files: list[PurePosixPath] = []
    for path in sorted(templates.rglob("*")):
        relative = PurePosixPath(path.relative_to(templates).as_posix())
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(relative)
    return files


def init_vault(
    target: Path, layout: VaultLayout, templates: Path = TEMPLATES_DIR
) -> InitResult:
    """Copy the template tree into ``target`` and return what was created and skipped."""
    target = Path(target).expanduser().resolve()
    _refuse_repo(target)
    _make_target(target)
    result = InitResult()
    for relative in _template_files(templates):
        destination = _destination(relative, layout)
        name = destination.as_posix()
        _make_folders(target, destination.parent)
        path = target.joinpath(*destination.parts)
        if path.exists() or path.is_symlink():
            result.skipped.append(name)
            continue
        text = (templates / relative).read_text(encoding="utf-8")
        try:
            atomic_create_text(path, text, mode=FILE_MODE)
        except FileExistsError:
            result.skipped.append(name)
            continue
        result.created.append(name)
    return result
