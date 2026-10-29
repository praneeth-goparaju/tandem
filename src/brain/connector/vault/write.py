"""Add to notes and create notes in the vault.

These are the plain functions behind the ``vault_append`` and ``vault_create``
tools. Every path goes through ``check_writable``, and every write goes through
``coordinated_write`` with the hash of what was read, so a note that changed in
between, for example an edit from the phone that iCloud just delivered, is
refused instead of overwritten. After writing, the note is read again and that
read is returned.

Notes are decoded as strict UTF-8 before they are rebuilt. A note that is not
valid UTF-8 is refused, so no byte of it is ever replaced.

``append`` adds text at the end of a note, or at the end of a heading's
section. A heading matches when a line is ``#`` to ``######``, a space, and
exactly that title. Lines inside the frontmatter or a fenced code block are
never headings. The first matching heading wins; when none matches, the note
gets a new ``## Heading`` at the end. The new text is always separated from what
comes before it by one blank line.

``create`` refuses a name that any note in the vault already has, comparing
names in NFC and ignoring case, as Obsidian does when it resolves links. A note
that iCloud has offloaded counts as existing.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath

from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout, owner_only_paths
from src.brain.connector.vault.coordination import coordinated_write
from src.brain.connector.vault.paths import check_writable, resolve_in_vault
from src.brain.connector.vault.read import (
    NotDownloaded,
    ReadResult,
    _split_lines,
    read_note,
)

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ICLOUD_SUFFIX = ".icloud"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCES = ("```", "~~~")


class NameExists(Exception):
    """Another note in the vault already has this name."""

    def __init__(self, path: str) -> None:
        super().__init__(f"A note named {PurePosixPath(path).name} already exists at {path}")
        self.path = path


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def daily_note_path(now: datetime, layout: VaultLayout = DEFAULT_LAYOUT) -> str:
    """Return the vault path of the daily note for ``now``'s date."""
    return layout.daily_note_path(now)


def render_template(template: str, now: datetime) -> str:
    """Fill the two date fields of the daily note template.

    ``{{date:YYYY-MM-DD}}`` becomes ``2026-10-22`` and
    ``{{date:dddd, YYYY-MM-DD}}`` becomes ``Thursday, 2026-10-22``, with English
    day names whatever the system language. Everything else stays as it is.
    """
    day = f"{now:%Y-%m-%d}"
    return template.replace(
        "{{date:dddd, YYYY-MM-DD}}", f"{DAY_NAMES[now.weekday()]}, {day}"
    ).replace("{{date:YYYY-MM-DD}}", day)


def _read_strict(path: Path, rel: str) -> tuple[str, str]:
    """Return the note's text and the SHA-256 of its bytes."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{rel} is not valid UTF-8, so it was not changed") from exc
    return text, hashlib.sha256(raw).hexdigest()


def _refuse_placeholder(path: Path, rel: str) -> None:
    if path.with_name(f".{path.name}{ICLOUD_SUFFIX}").exists():
        raise NotDownloaded(f"{rel} is in iCloud and not downloaded to this Mac yet")


def _read_template(vault: Path, layout: VaultLayout) -> str:
    template = layout.template_path
    path = resolve_in_vault(vault, template)
    if not path.exists():
        _refuse_placeholder(path, template)
        raise FileNotFoundError(f"the daily note template {template} is missing")
    text, _ = _read_strict(path, template)
    return text


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (line index, level, title) for each heading line."""
    found: list[tuple[int, int, str]] = []
    start = 0
    if lines and lines[0].rstrip() == "---":
        for index in range(1, len(lines)):
            if lines[index].rstrip() == "---":
                start = index + 1
                break
    fence: str | None = None
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        opening = next((mark for mark in _FENCES if stripped.startswith(mark)), None)
        if opening is not None:
            fence = opening
            continue
        match = _HEADING.match(lines[index].rstrip("\r\n"))
        if match:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def _after(lines: list[str], block: str) -> str:
    """Join ``lines`` and ``block`` with exactly one blank line between them."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if end == 0:
        return block
    text = "".join(lines[:end])
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block


def _heading_title(heading: str) -> str:
    match = _HEADING.match(heading.strip())
    title = match.group(2) if match else heading.strip()
    if not title:
        raise ValueError("the heading is empty")
    return title


def _add_text(note: str, text: str, heading: str | None) -> str:
    block = text.strip("\n") + "\n"
    lines = _split_lines(note)
    if heading is None:
        return _after(lines, block)
    title = _heading_title(heading)
    headings = _headings(lines)
    first = next(((index, level) for index, level, name in headings if name == title), None)
    if first is None:
        return _after(lines, f"## {title}\n\n{block}")
    index, level = first
    end = next(
        (later for later, deeper, _ in headings if later > index and deeper <= level),
        len(lines),
    )
    rest = lines[end:]
    return _after(lines[:end], block) + ("\n" if rest else "") + "".join(rest)


def _read_back(vault: Path, rel: str, written_hash: str) -> ReadResult:
    result = read_note(vault, rel)
    if result.hash != written_hash:
        raise OSError(f"{rel} changed right after it was written; read it again")
    return result


def append(
    vault: Path,
    rel: str,
    text: str,
    heading: str | None = None,
    now: datetime | None = None,
    *,
    layout: VaultLayout = DEFAULT_LAYOUT,
) -> ReadResult:
    """Add ``text`` to the end of a note, or to the end of ``heading``'s section.

    Today's daily note is created from the template when it does not exist;
    any other missing note raises ``FileNotFoundError``. Raises ``StaleVersion``
    when the note changed after it was read.
    """
    if not text.strip():
        raise ValueError("there is no text to append")
    when = now if now is not None else datetime.now().astimezone()
    path = check_writable(vault, rel, owner_only=owner_only_paths(layout))
    expected_hash: str | None
    if path.exists():
        note, expected_hash = _read_strict(path, rel)
    else:
        _refuse_placeholder(path, rel)
        if path != resolve_in_vault(vault, layout.daily_note_path(when)):
            raise FileNotFoundError(rel)
        note, expected_hash = render_template(_read_template(vault, layout), when), None
        path.parent.mkdir(parents=True, exist_ok=True)
    written = coordinated_write(path, _add_text(note, text, heading), expected_hash)
    return _read_back(vault, rel, written)


def _existing_with_name(vault: Path, name: str) -> None:
    """Raise if a note anywhere in the vault already has ``name``."""
    root = vault.expanduser().resolve(strict=False)
    wanted = _nfc(name).casefold()
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for file in sorted(files):
            offloaded = file.startswith(".") and file.endswith(ICLOUD_SUFFIX)
            stored = file[1 : -len(ICLOUD_SUFFIX)] if offloaded else file
            if stored.startswith(".") or _nfc(stored).casefold() != wanted:
                continue
            found = _nfc((Path(here) / stored).relative_to(root).as_posix())
            if offloaded:
                raise NotDownloaded(f"{found} is in iCloud and not downloaded to this Mac yet")
            raise NameExists(found)


def _refuse_odd_name(name: str) -> None:
    folded = name.casefold()
    if name.startswith("."):
        raise ValueError(f"{name} starts with a dot; a new note's name may not")
    if folded.endswith(ICLOUD_SUFFIX):
        raise ValueError(f"{name} ends in .icloud, which is how iCloud marks offloaded notes")
    if not folded.endswith(".md"):
        raise ValueError(f"{name} does not end in .md; only notes can be created")


def create(vault: Path, rel: str, text: str, *, layout: VaultLayout = DEFAULT_LAYOUT) -> ReadResult:
    """Create a new note holding ``text``.

    Only a Markdown note can be created: the name must end in ``.md`` and may
    not start with a dot or end in ``.icloud``, so no script, settings file or
    fake iCloud placeholder is made. Raises ``NameExists`` when any note in the
    vault already has the name, and ``NotDownloaded`` when that note is
    offloaded to iCloud. Missing folders on the way are created.
    """
    path = check_writable(vault, rel, owner_only=owner_only_paths(layout))
    if path.is_dir():
        raise IsADirectoryError(f"{rel or '.'} is a folder, not a note")
    _refuse_odd_name(PurePosixPath(_nfc(rel)).name)
    _existing_with_name(vault, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = coordinated_write(path, text, None)
    return _read_back(vault, rel, written)
