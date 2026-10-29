"""Change existing text in a note: one exact passage at a time, with undo.

These are the plain functions behind the ``vault_edit``, ``vault_edit_confirm``
and ``vault_undo`` tools. An edit replaces one passage, ``old``, that must
appear exactly once in the note, with ``new``. The chat sends the hash from
its ``vault_read`` of the note and a short reason.

Fixed rules in ``decide`` say whether the edit runs at once or waits for the
owner's Approve button. Mail and web pages can carry planted instructions, so
the rules hold anything large or structural, and every edit from a chat that
read mail in the last 30 minutes. An edit runs without a tap only when all of
these hold:

- the new text, and the note where it went in, use no line ending other than
  ``\\n`` and ``\\r\\n``. The connector's reader splits lines on ``\\n`` only,
  while YAML and Markdown also end lines at a lone ``\\r`` and PyYAML at
  ``\\x85``, ``\\u2028`` and ``\\u2029``, so such characters could hide or plant
  frontmatter keys and headings;
- it changes at most 10 lines;
- it removes at most a quarter of the note's lines (a line that is replaced
  counts as removed);
- it touches no heading. A heading line is one starting with ``#`` after
  any spaces or tabs, or a line of only ``=`` or ``-`` characters (spaces
  around them allowed) under a non-blank line outside the frontmatter. The
  lines looked at are the changed lines, the line right after each changed
  block (its heading status depends on the line above), the whole lines the
  passage and the new text lie on, and the passage and new text themselves;
- it leaves the frontmatter, the leading ``---`` block, as it was, except the
  value of ``status`` in notes under the actions folder of the vault layout;
- fewer than 20 edits ran in the last 60 minutes;
- the chat has not read mail in the last 30 minutes.

Lines are counted by comparing the whole note before and after the edit with
``difflib``. YAML and Markdown end a line at ``\\r\\n``, ``\\r`` or ``\\n``, so all
three are turned into ``\\n`` before anything is counted; otherwise a lone
``\\r`` could hide extra lines from every rule. Every edit that runs is
recorded in the undo store with the note's hash after the edit. ``undo`` puts
the old passage back only while the note still has that hash, so it never
overwrites a later change.
"""

from __future__ import annotations

import difflib
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from src.brain.connector.layout import DEFAULT_LAYOUT
from src.brain.connector.vault.coordination import StaleVersion, coordinated_write
from src.brain.connector.vault.paths import OWNER_ONLY, check_writable
from src.brain.connector.vault.read import ReadResult, _split_lines
from src.brain.connector.vault.undo_store import UndoStore
from src.brain.connector.vault.write import _read_back, _read_strict, _refuse_placeholder

MAX_LINES = 10
MAX_EDITS_PER_HOUR = 20
MIN_REASON_WORDS = 3
FRONTMATTER_FENCE = "---"

_STATUS_LINE = re.compile(r"^status:(?:[ \t][^\r\n]*)?$")
_UNUSUAL_LINE_END = re.compile("\r(?!\n)|[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_SETEXT_UNDERLINE = re.compile(r"[ \t]*[=-]+[ \t]*")


class EditRefused(Exception):
    """The edit, held edit or undo cannot run; the message says why."""


@dataclass
class Decision:
    allow: bool
    reason: str


@dataclass
class EditResult:
    status: Literal["done", "held"]
    edit_id: str | None
    held_id: str | None
    reason: str
    read_back: ReadResult | None


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def _frontmatter(text: str) -> list[str] | None:
    """Return the lines of the leading ``---`` block, fences included."""
    lines = _split_lines(text.removeprefix("﻿"))
    if not lines or _bare(lines[0]).rstrip() != FRONTMATTER_FENCE:
        return None
    for end in range(1, len(lines)):
        if _bare(lines[end]).rstrip() == FRONTMATTER_FENCE:
            return lines[: end + 1]
    return None


def _in_actions(rel: str, actions_dir: str) -> bool:
    parts = posixpath.normpath(unicodedata.normalize("NFC", rel)).casefold().split("/")
    folder = posixpath.normpath(unicodedata.normalize("NFC", actions_dir)).casefold().split("/")
    return len(parts) > len(folder) and parts[: len(folder)] == folder


def _frontmatter_kept(rel: str, before: str, after: str, actions_dir: str) -> bool:
    old_block, new_block = _frontmatter(before), _frontmatter(after)
    if old_block == new_block:
        return True
    if old_block is None or new_block is None or len(old_block) != len(new_block):
        return False
    if not _in_actions(rel, actions_dir):
        return False
    return all(
        old == new or (_STATUS_LINE.match(_bare(old)) and _STATUS_LINE.match(_bare(new)))
        for old, new in zip(old_block, new_block, strict=True)
    )


def _one_line_end(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _touched_lines(text: str, start: int, end: int) -> range:
    """Return the indexes of the lines of ``text`` that ``start:end`` lie on."""
    if start >= end:
        return range(0)
    return range(text.count("\n", 0, start), text.count("\n", 0, end - 1) + 1)


def _starts_heading(line: str) -> bool:
    return line.lstrip(" \t").startswith("#")


def _is_heading(lines: list[str], index: int, frontmatter_lines: int) -> bool:
    line = _bare(lines[index])
    if _starts_heading(line):
        return True
    return (
        index >= max(frontmatter_lines, 1)
        and _SETEXT_UNDERLINE.fullmatch(line) is not None
        and bool(lines[index - 1].strip())
    )


def _unusual_at_join(after: str, index: int | None, new: str) -> bool:
    """Say whether the edited note has an unusual line ending where ``new`` went in.

    Deleting the ``\\n`` of a ``\\r\\n`` leaves a lone ``\\r`` without one in
    ``new``. The search runs over the whole text so the check sees the
    character after the window.
    """
    if index is None:
        return False
    found = _UNUSUAL_LINE_END.search(after, max(index - 1, 0))
    return found is not None and found.start() <= index + len(new)


def _first_index(note: str, old: str) -> int | None:
    index = note.find(old) if old else -1
    return None if index == -1 else index


def decide(
    rel: str,
    note_text: str,
    old: str,
    new: str,
    edits_last_hour: int,
    mail_read_recently: bool,
    *,
    actions_dir: str = DEFAULT_LAYOUT.actions_dir,
) -> Decision:
    """Say whether replacing ``old`` with ``new`` in ``note_text`` may run without a tap.

    When the edit is held, ``reason`` names every rule it breaks, joined by
    ``"; "``.
    """
    index = _first_index(note_text, old)
    if index is None:
        raw_before, raw_after, raw_start = old, new, 0
    else:
        raw_before = note_text
        raw_after = note_text[:index] + new + note_text[index + len(old) :]
        raw_start = index
    before, after = _one_line_end(raw_before), _one_line_end(raw_after)
    before_lines, after_lines = _split_lines(before), _split_lines(after)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    before_seen: set[int] = set()
    after_seen: set[int] = set()
    replaced = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        replaced += max(i2 - i1, j2 - j1)
        removed += i2 - i1
        before_seen.update(range(i1, min(i2 + 1, len(before_lines))))
        after_seen.update(range(j1, min(j2 + 1, len(after_lines))))
    start = len(_one_line_end(raw_before[:raw_start]))
    before_seen.update(
        _touched_lines(before, start, len(_one_line_end(raw_before[: raw_start + len(old)])))
    )
    after_seen.update(
        _touched_lines(after, start, len(_one_line_end(raw_after[: raw_start + len(new)])))
    )
    before_front = len(_frontmatter(before) or [])
    after_front = len(_frontmatter(after) or [])
    touches_heading = (
        any(_is_heading(before_lines, i, before_front) for i in before_seen)
        or any(_is_heading(after_lines, i, after_front) for i in after_seen)
        or any(
            _starts_heading(line)
            for line in _split_lines(_one_line_end(old)) + _split_lines(_one_line_end(new))
        )
    )

    broken: list[str] = []
    if _UNUSUAL_LINE_END.search(new) or _unusual_at_join(raw_after, index, new):
        broken.append("uses an unusual line ending")
    if replaced > MAX_LINES:
        broken.append(f"replaces {replaced} lines, the limit is {MAX_LINES}")
    total = len(before_lines)
    if total and removed * 4 > total:
        share = -(-removed * 100 // total)
        broken.append(f"removes {share}% of the note, the limit is a quarter")
    if touches_heading:
        broken.append("touches a heading")
    if not _frontmatter_kept(rel, before, after, actions_dir):
        broken.append("changes frontmatter")
    if edits_last_hour >= MAX_EDITS_PER_HOUR:
        broken.append(f"{edits_last_hour} edits already ran in the last hour")
    if mail_read_recently:
        broken.append("this chat read mail in the last 30 minutes")
    if broken:
        return Decision(allow=False, reason="; ".join(broken))
    return Decision(allow=True, reason="small edit within the rules")


def _count(note: str, passage: str) -> int:
    """Count where ``passage`` starts in ``note``, overlapping places included."""
    found, index = 0, note.find(passage)
    while index != -1:
        found += 1
        index = note.find(passage, index + 1)
    return found


def _read_current(vault: Path, rel: str, owner_only: tuple[str, ...]) -> tuple[Path, str, str]:
    path = check_writable(vault, rel, owner_only=owner_only)
    if not path.exists():
        _refuse_placeholder(path, rel)
        raise FileNotFoundError(rel)
    text, current = _read_strict(path, rel)
    return path, text, current


def _write_edit(
    vault: Path,
    store: UndoStore,
    path: Path,
    rel: str,
    text: str,
    old: str,
    new: str,
    expected_hash: str,
) -> tuple[str, ReadResult]:
    index = text.index(old)
    written = coordinated_write(path, text[:index] + new + text[index + len(old) :], expected_hash)
    edit_id = store.record(rel, old, new, written, index)
    return edit_id, _read_back(vault, rel, written)


def apply_edit(
    vault: Path,
    store: UndoStore,
    rel: str,
    old: str,
    new: str,
    expected_hash: str,
    reason: str,
    mail_read_recently: bool,
    *,
    actions_dir: str = DEFAULT_LAYOUT.actions_dir,
    owner_only: tuple[str, ...] = OWNER_ONLY,
) -> EditResult:
    """Replace the one place ``old`` appears in the note with ``new``, or hold the edit.

    Raises ``PathRefused`` for a path a chat may not write, ``StaleVersion``
    when the note's hash is not ``expected_hash`` or the note changes while it
    is written, and ``EditRefused`` when ``reason`` is shorter than a few words
    or ``old`` is empty, missing or appears more than once.
    """
    path, text, current = _read_current(vault, rel, owner_only)
    if len(reason.split()) < MIN_REASON_WORDS:
        raise EditRefused("give a reason of a few words for this edit")
    if current != expected_hash:
        raise StaleVersion(f"{rel} changed after it was read; read it again")
    if not old:
        raise EditRefused("the passage to replace is empty")
    found = _count(text, old)
    if found == 0:
        raise EditRefused(f"the passage was not found in {rel}")
    if found > 1:
        raise EditRefused(
            f"the passage appears {found} times in {rel}; include more of the text around it"
        )
    edits_last_hour = store.edits_since(datetime.now(UTC) - timedelta(hours=1))
    decision = decide(
        rel, text, old, new, edits_last_hour, mail_read_recently, actions_dir=actions_dir
    )
    if not decision.allow:
        held_id = store.hold(rel, old, new, expected_hash, reason)
        return EditResult(
            status="held", edit_id=None, held_id=held_id, reason=decision.reason, read_back=None
        )
    edit_id, read_back = _write_edit(vault, store, path, rel, text, old, new, expected_hash)
    return EditResult(
        status="done", edit_id=edit_id, held_id=None, reason=decision.reason, read_back=read_back
    )


def confirm_held(
    vault: Path,
    store: UndoStore,
    held_id: str,
    *,
    owner_only: tuple[str, ...] = OWNER_ONLY,
) -> EditResult:
    """Run an edit the rules held, once the owner has approved it.

    Raises ``StaleVersion`` when the note changed since the edit was held; the
    held edit then stays in the store. Raises ``EditRefused`` when there is no
    held edit with this id.
    """
    try:
        held = store.take_held(held_id)
    except KeyError:
        raise EditRefused(f"there is no held edit {held_id}; it may have run already") from None
    path, text, current = _read_current(vault, held.rel, owner_only)
    if current != held.expected_hash:
        raise StaleVersion(
            f"{held.rel} changed since this edit was held, so it was not run; "
            "read the note and ask for the edit again"
        )
    edit_id, read_back = _write_edit(
        vault, store, path, held.rel, text, held.old, held.new, held.expected_hash
    )
    store.drop_held(held_id)
    return EditResult(
        status="done",
        edit_id=edit_id,
        held_id=held_id,
        reason="approved by the owner",
        read_back=read_back,
    )


def undo(
    vault: Path,
    store: UndoStore,
    edit_id: str,
    *,
    owner_only: tuple[str, ...] = OWNER_ONLY,
) -> ReadResult:
    """Put back the passage an edit replaced and return the note as read back.

    Raises ``EditRefused`` when there is no edit with this id or the note
    changed since the edit, and ``StaleVersion`` when it changes while the
    undo is written.
    """
    try:
        row = store.get(edit_id)
    except KeyError:
        raise EditRefused(f"there is no edit {edit_id} to undo") from None
    path, text, current = _read_current(vault, row.rel, owner_only)
    end = row.start + len(row.new)
    if current != row.hash_after or text[row.start : end] != row.new:
        raise EditRefused(
            f"{row.rel} changed since the edit, so it was not undone; "
            "read the note and change it back with a new edit"
        )
    written = coordinated_write(path, text[: row.start] + row.old + text[end:], current)
    return _read_back(vault, row.rel, written)
