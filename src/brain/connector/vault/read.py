"""Search, read and list notes in the vault.

These are the plain functions behind the ``vault_search``, ``vault_read`` and
``vault_list`` tools. Every path from a chat goes through ``resolve_in_vault``.

Search is a substring match on lines of Markdown notes. Both the query and each
line are normalised to NFC and case-folded, so ``führerschein`` finds
``Führerschein`` whether the note was stored in NFC or NFD, while
``fuhrerschein`` does not. Folders and files whose name starts with ``.`` are
skipped. That covers ``.obsidian``, ``.trash`` and ``.claude``, and also the
``.X.md.icloud`` placeholder iCloud leaves for a note it has offloaded.

Results use vault-relative paths with ``/`` separators in NFC. Line numbers are
1-based and count lines ended by ``\\n``.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

import yaml

from src.brain.connector.vault.paths import resolve_in_vault

NOTE_SUFFIX = ".md"
FRONTMATTER_FENCE = "---"


class NotDownloaded(Exception):
    """The note exists only as an iCloud placeholder and is not on this Mac yet."""


@dataclass
class Match:
    path: str
    line: int
    text: str


@dataclass
class ReadResult:
    path: str
    text: str
    hash: str
    total_lines: int


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(text: str) -> str:
    return _nfc(_nfc(text).casefold())


def _root(vault: Path) -> Path:
    return vault.expanduser().resolve(strict=False)


def _relative(path: Path, root: Path) -> str:
    return _nfc(PurePosixPath(path.relative_to(root).as_posix()).as_posix())


def _relative_or_none(path: Path, root: Path) -> PurePosixPath | None:
    try:
        return PurePosixPath(
            PurePath(_nfc(str(path))).relative_to(PurePath(_nfc(str(root)))).as_posix()
        )
    except ValueError:
        return None


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping the line ends."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _frontmatter(text: str) -> dict[object, object] | None:
    """Return the leading ``---`` block as a dict, or None if absent or broken."""
    lines = _split_lines(text.removeprefix("﻿"))
    if not lines or lines[0].rstrip() != FRONTMATTER_FENCE:
        return None
    for end, line in enumerate(lines[1:], start=1):
        if line.rstrip() == FRONTMATTER_FENCE:
            try:
                data = yaml.safe_load("".join(lines[1:end]))
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def _has_field(text: str, field: tuple[str, str]) -> bool:
    data = _frontmatter(text)
    if data is None:
        return False
    key, wanted = field
    if key not in data:
        return False
    return _nfc(str(data[key])) == _nfc(wanted)


def _note_files(start: Path, root: Path) -> list[Path]:
    """Find notes under ``start`` by the names they have on disk.

    A file whose real path leaves the vault, through a symlink, is skipped.
    """
    found: list[Path] = []
    for here, dirs, files in os.walk(start):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or not name.endswith(NOTE_SUFFIX):
                continue
            path = Path(here) / name
            real = path.resolve(strict=False)
            if _relative_or_none(real, root) is None:
                continue
            found.append(real)
    return found


def search(
    vault: Path,
    query: str,
    folder: str | None = None,
    field: tuple[str, str] | None = None,
    limit: int = 50,
) -> list[Match]:
    """Find lines containing ``query`` in the vault's notes.

    ``folder`` limits the search to one vault folder. ``field`` keeps only notes
    whose frontmatter has that key with that value; a note whose frontmatter
    cannot be parsed never matches. At most ``limit`` matches are returned.
    A folder whose own path has a part starting with ``.`` is not searched.

    With an empty ``query`` and a ``field``, every matching note is listed once,
    at line 1 with its first line. An empty ``query`` alone finds nothing.
    """
    start = resolve_in_vault(vault, folder or "")
    root = _root(vault)
    wanted = _fold(query)
    matches: list[Match] = []
    if limit <= 0 or (not wanted and field is None) or not start.is_dir():
        return matches
    inside = _relative_or_none(start, root)
    if inside is None or any(part.startswith(".") for part in inside.parts):
        return matches
    for path in _note_files(start, root):
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if field is not None and not _has_field(text, field):
            continue
        rel = _relative(path, root)
        if not wanted:
            first = _split_lines(text)[:1]
            matches.append(
                Match(path=rel, line=1, text=_nfc(first[0].rstrip("\r\n")) if first else "")
            )
            if len(matches) >= limit:
                return matches
            continue
        for number, line in enumerate(_split_lines(text), start=1):
            if wanted in _fold(line):
                matches.append(Match(path=rel, line=number, text=_nfc(line.rstrip("\r\n"))))
                if len(matches) >= limit:
                    return matches
    return matches


def read_note(
    vault: Path,
    rel: str,
    start: int | None = None,
    end: int | None = None,
) -> ReadResult:
    """Read a note, or the lines ``start`` to ``end`` (1-based, inclusive).

    ``hash`` is the SHA-256 of the whole file's bytes even when a range is
    asked for, and ``total_lines`` counts the whole file. Raises
    ``ValueError`` when ``start`` or ``end`` is below 1, and ``NotDownloaded``
    when only the iCloud placeholder is on disk.
    """
    if (start is not None and start < 1) or (end is not None and end < 1):
        raise ValueError("start and end are 1-based line numbers")
    path = resolve_in_vault(vault, rel)
    if not path.exists():
        if path.with_name(f".{path.name}.icloud").exists():
            raise NotDownloaded(f"{rel} is in iCloud and not downloaded to this Mac yet")
        raise FileNotFoundError(rel)
    raw = path.read_bytes()
    lines = _split_lines(raw.decode("utf-8", errors="replace"))
    first = start if start is not None else 1
    last = end if end is not None else len(lines)
    return ReadResult(
        path=_relative(path, _root(vault)),
        text="".join(lines[first - 1 : last]),
        hash=hashlib.sha256(raw).hexdigest(),
        total_lines=len(lines),
    )


def list_folder(vault: Path, rel: str = "") -> list[str]:
    """List a vault folder: sorted names, folders with a trailing ``/``.

    Names starting with ``.`` are hidden, which also hides iCloud placeholders.
    """
    path = resolve_in_vault(vault, rel)
    if not path.is_dir():
        raise FileNotFoundError(rel)
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            names.append(_nfc(entry.name) + ("/" if entry.is_dir() else ""))
    return sorted(names)
