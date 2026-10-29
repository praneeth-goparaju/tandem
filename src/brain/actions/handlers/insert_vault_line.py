from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from src.brain.actions.outcomes import permanent_failure, retryable_failure, success
from src.brain.actions.types import validate_vault_note_path
from src.brain.atomic_io import (
    StaleWriteError,
    atomic_write_text_if_sha256,
    confined_path,
    read_regular_text,
)

MAX_LINE_CHARS = 2000
_MAX_REPORTED_MATCHES = 10
_HEADING = re.compile(r"#{1,6}\s+\S")


@dataclass(frozen=True)
class LineInsertion:
    content: str
    anchor_line: int
    section: str | None
    already_present: bool


def validate_single_line(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_LINE_CHARS
        or value.splitlines() != [value]
    ):
        raise ValueError(
            f"{name} must be one line of non-empty text without line breaks, "
            f"at most {MAX_LINE_CHARS} characters"
        )
    return value


def insert_after_anchor(existing: str, anchor: object, text: object) -> LineInsertion:
    """Insert ``text`` as a new line directly after the one line equal to ``anchor``.

    Raises ``ValueError`` when the anchor matches no line or several. When the
    line after the anchor already equals ``text`` the body is returned as it is
    with ``already_present`` set. The trailing newline of ``existing`` is kept
    as it was.
    """
    wanted = validate_single_line(anchor, "anchor").rstrip()
    new_line = validate_single_line(text, "text")

    lines = existing.split("\n")
    matches = [index for index, line in enumerate(lines) if line.rstrip() == wanted]
    if len(matches) != 1:
        numbers = ", ".join(str(index + 1) for index in matches[:_MAX_REPORTED_MATCHES])
        if len(matches) > _MAX_REPORTED_MATCHES:
            numbers += ", …"
        where = f" ({numbers})" if matches else ""
        raise ValueError(
            f"anchor matches {len(matches)} lines{where}; "
            "it must match exactly one whole line"
        )

    index = matches[0]
    section = next(
        (
            line.strip()
            for line in reversed(lines[:index])
            if _HEADING.match(line)
        ),
        None,
    )
    if index + 1 < len(lines) and lines[index + 1].rstrip() == new_line.rstrip():
        return LineInsertion(existing, index + 1, section, True)

    lines.insert(index + 1, new_line)
    return LineInsertion("\n".join(lines), index + 1, section, False)


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    from src.orchestrator.config import VAULT_PATH

    try:
        rel = validate_vault_note_path(payload.get("path"))
        target = confined_path(VAULT_PATH, rel)
    except ValueError as exc:
        return permanent_failure(f"unsafe vault note path: {exc}")

    try:
        existing = read_regular_text(target)
    except FileNotFoundError:
        return permanent_failure(f"{rel} does not exist")
    except (OSError, ValueError) as exc:
        return permanent_failure(f"unsafe or unreadable vault target: {exc}")

    try:
        insertion = insert_after_anchor(
            existing, payload.get("anchor"), payload.get("text")
        )
    except ValueError as exc:
        return permanent_failure(f"{rel}: {exc}")
    if insertion.already_present:
        return success("unchanged (idempotent no-op)")

    try:
        atomic_write_text_if_sha256(
            target, insertion.content, hashlib.sha256(existing.encode()).hexdigest()
        )
    except (FileExistsError, StaleWriteError):
        return retryable_failure(f"{rel} changed while the line was being added")
    return success(f"added one line to {rel} after line {insertion.anchor_line}")
