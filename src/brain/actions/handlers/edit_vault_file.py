from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.brain.actions.outcomes import retryable_failure
from src.brain.actions.types import (
    validate_expected_file_sha256,
    validate_vault_note_path,
)
from src.brain.atomic_io import (
    StaleWriteError,
    atomic_write_text_if_sha256,
    confined_path,
    read_regular_text,
    regular_file_sha256,
)


def _read_existing(target: Path) -> str | None:
    try:
        return read_regular_text(target)
    except FileNotFoundError:
        return None


def _extract_section(text: str, name: str) -> str | None:
    pattern = re.compile(
        rf"^##\s+{re.escape(name)}\s*$.*?(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(0).rstrip() + "\n" if m else None


def compute_final_content(
    target: Path, new_content: str, preserve_sections: Any = None
) -> str:
    """The final on-disk body for one vault file: `new_content` with any named
    user-authored sections re-appended from the existing file.

    Factored out so the batch door (`edit_vault_files`) applies IDENTICAL
    per-file semantics — the protected-block behavior (e.g. `## Quick Capture`)
    must be no weaker in a cascade than in the single-file door. A section is
    only re-appended when the existing file has it AND the new content does not
    already redefine it.
    """
    return merge_final_content(
        _read_existing(target), new_content, preserve_sections
    )


def merge_final_content(
    existing: str | None, new_content: str, preserve_sections: Any = None
) -> str:
    if preserve_sections is not None and (
        not isinstance(preserve_sections, list)
        or any(not isinstance(name, str) or not name for name in preserve_sections)
    ):
        raise ValueError("preserve_sections must be a list of non-empty strings")
    preserve = preserve_sections or []
    final = new_content
    if existing is not None and preserve:
        kept_blocks = []
        for name in preserve:
            block = _extract_section(existing, name)
            if block and f"## {name}" not in new_content:
                kept_blocks.append(block)
        if kept_blocks:
            sep = "" if final.endswith("\n") else "\n"
            final = final + sep + "\n" + "\n".join(kept_blocks)
    return final


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    from src.orchestrator.config import VAULT_PATH

    new_content = payload.get("new_content")
    if not isinstance(new_content, str):
        return {"ok": False, "result": "missing new_content (expected a string)"}

    try:
        rel = validate_vault_note_path(payload.get("path"))
        expected = validate_expected_file_sha256(payload.get("expected_sha256"))
        target = confined_path(VAULT_PATH, rel)
    except ValueError as exc:
        return {"ok": False, "result": f"unsafe vault note path: {exc}"}

    try:
        if regular_file_sha256(target) != expected:
            return retryable_failure(f"{rel} changed after approval; review again")
        final = compute_final_content(
            target,
            new_content,
            payload.get("preserve_sections"),
        )
        existing = _read_existing(target)
    except OSError as exc:
        return {"ok": False, "result": f"unsafe or unreadable vault target: {exc}"}

    if existing == final:
        return {"ok": True, "result": "unchanged (idempotent no-op)"}

    try:
        atomic_write_text_if_sha256(target, final, expected)
    except (FileExistsError, StaleWriteError):
        return retryable_failure(f"{rel} changed after approval; review again")
    return {"ok": True, "result": f"wrote {rel} ({len(final)} bytes)"}
