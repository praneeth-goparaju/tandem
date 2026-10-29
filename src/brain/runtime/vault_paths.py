from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from pathlib import PurePosixPath


def _fold(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)


def names_path(parts: Sequence[str], entry: str, *, fold: bool = False) -> bool:
    """True when the vault-relative `parts` are `entry`, or inside it when `entry` is a folder.

    With `fold`, names compare ignoring case and Unicode form.
    """
    given = tuple(parts)
    wanted = PurePosixPath(entry).parts
    if fold:
        given, wanted = _fold(given), _fold(wanted)
    if not wanted:
        return False
    if entry.endswith("/"):
        return given[: len(wanted)] == wanted
    return given == wanted


def path_in_vault_paths(relative_path: str, entries: Sequence[str]) -> bool:
    """True when the vault-relative `relative_path` is one of `entries` or inside a folder entry.

    The path must be one canonical relative spelling: not empty, not absolute,
    no `.`, `..` or empty segment. Anything else is False, whatever a caller
    checks later. Names compare exactly, so a different spelling of an allowed
    path is refused.
    """
    if not relative_path or relative_path.startswith("/"):
        return False
    trimmed = relative_path.rstrip("/")
    if any(segment in {"", ".", ".."} for segment in trimmed.split("/")):
        return False
    path = PurePosixPath(relative_path)
    if path.as_posix() != trimmed:
        return False
    return any(names_path(path.parts, entry) for entry in entries)
