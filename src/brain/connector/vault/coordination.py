"""Write one vault file safely: atomically, and only if nobody changed it first.

``coordinated_write`` checks the file's SHA-256 against the hash the caller
read, then replaces the file through a temporary file in the same folder, with
the existing file's permissions kept. On macOS the check and the write run
inside an ``NSFileCoordinator`` write, so iCloud and Obsidian, which take part
in file coordination, cannot change the file between the check and the write.
Updating an existing file coordinates "for merging", which first asks the
other apps to save any change they still hold, so that change is caught by the
hash check. Only a new file coordinates "for replacing", because that option
tells the other apps the old item is going away.
Where Foundation cannot be imported, the same check and write run without it.

The write goes to the name the folder already stores. APFS finds a file under
its NFC or NFD spelling and, on the usual volume, under any upper or lower
case, but replacing onto a different spelling would rename the stored file,
which iCloud and Obsidian would see as a rename.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Callable
from pathlib import Path

from src.brain.atomic_io import StaleWriteError, atomic_write_text_if_sha256

try:
    from Foundation import (
        NSURL,
        NSFileCoordinator,
        NSFileCoordinatorWritingForMerging,
        NSFileCoordinatorWritingForReplacing,
    )
except ImportError:
    NSFileCoordinator = None


class StaleVersion(Exception):
    """The file changed after it was read, so the write was not made."""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def on_disk_path(path: Path) -> Path:
    """Return ``path`` with its file name spelled the way the folder stores it.

    A name that is not stored yet is returned as given.
    """
    try:
        names = os.listdir(path.parent)
    except OSError:
        return path
    if path.name in names:
        return path
    wanted = _nfc(path.name)
    for name in names:
        if _nfc(name) == wanted:
            return path.parent / name
    if path.exists():
        folded = wanted.casefold()
        for name in names:
            if _nfc(name).casefold() == folded:
                return path.parent / name
    return path


def _coordinated(path: Path, work: Callable[[], None], option: int) -> None:
    failure: list[BaseException] = []
    ran: list[bool] = []

    def accessor(_new_url: object) -> None:
        ran.append(True)
        try:
            work()
        except BaseException as exc:
            failure.append(exc)

    coordinator = NSFileCoordinator.alloc().initWithFilePresenter_(None)
    error = coordinator.coordinateWritingItemAtURL_options_error_byAccessor_(
        NSURL.fileURLWithPath_(str(path)),
        option,
        None,
        accessor,
    )
    if failure:
        raise failure[0]
    if not ran:
        reason = error.localizedDescription() if error is not None else "no reason given"
        raise OSError(f"macOS did not allow writing {path.name}: {reason}")


def coordinated_write(path: Path, data: str, expected_hash: str | None) -> str:
    """Replace ``path`` with ``data`` if its hash is still ``expected_hash``.

    ``expected_hash`` of None means the file must not exist yet. Raises
    ``StaleVersion`` when the file on disk no longer matches. Returns the
    SHA-256 of the bytes written.
    """
    target = on_disk_path(path)

    def write() -> None:
        try:
            atomic_write_text_if_sha256(target, data, expected_hash)
        except (StaleWriteError, FileExistsError) as exc:
            raise StaleVersion(
                f"{target.name} changed after it was read, or is being written; read it again"
            ) from exc

    if NSFileCoordinator is None:
        write()
    elif expected_hash is None:
        _coordinated(target, write, NSFileCoordinatorWritingForReplacing)
    else:
        _coordinated(target, write, NSFileCoordinatorWritingForMerging)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
