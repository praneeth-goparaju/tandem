from __future__ import annotations

from pathlib import Path
from typing import Any

from src.brain.actions.handlers.edit_vault_file import (
    _read_existing,
    compute_final_content,
)
from src.brain.actions.outcomes import retryable_failure
from src.brain.actions.types import (
    MAX_VAULT_FILE_CHANGES as MAX_CHANGES,
)
from src.brain.actions.types import (
    validate_expected_file_sha256,
    validate_vault_note_path,
)
from src.brain.atomic_io import (
    StaleWriteError,
    commit_staged_if_sha256,
    confined_path,
    discard_staged,
    regular_file_sha256,
    stage_text,
)
from src.memory import store


def _cleanup_temps(temps: list[Path]) -> None:
    for tmp in temps:
        try:
            discard_staged(tmp)
        except OSError:
            pass


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    from src.orchestrator.config import VAULT_PATH

    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        return {"ok": False, "result": "missing changes (expected a non-empty list)"}
    if len(changes) > MAX_CHANGES:
        return {
            "ok": False,
            "result": (
                f"too many changes ({len(changes)} > {MAX_CHANGES}): a cascade is a "
                "handful of related files, not a sweep — split by topic into separate "
                "propose_action(type='edit_vault_files', ...) proposals"
            ),
        }

    planned: list[tuple[Path, str, str, str | None]] = []
    seen: set[str] = set()
    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            return {"ok": False, "result": f"change {i} is not an object"}

        try:
            rel = validate_vault_note_path(change.get("path"))
            expected = validate_expected_file_sha256(change.get("expected_sha256"))
            target = confined_path(VAULT_PATH, rel)
        except ValueError as exc:
            return {"ok": False, "result": f"change {i}: unsafe vault note path: {exc}"}

        new_content = change.get("new_content")
        if not isinstance(new_content, str):
            return {
                "ok": False,
                "result": f"change {i} ({rel}): missing new_content (expected a string)",
            }

        # casefold: the vault lives on case-insensitive APFS/iCloud, where
        # "Foo.md" and "foo.md" are the same file — treat them as duplicates.
        key = str(target).casefold()
        if key in seen:
            return {"ok": False, "result": f"duplicate path in payload: {rel}"}
        seen.add(key)

        try:
            if regular_file_sha256(target) != expected:
                return retryable_failure(f"{rel} changed after approval; review again")
            final = compute_final_content(
                target,
                new_content,
                change.get("preserve_sections"),
            )
        except OSError as exc:
            return {
                "ok": False,
                "result": f"change {i}: unsafe or unreadable vault target: {exc}",
            }
        planned.append((target, final, rel, expected))

    try:
        to_write = [
            (target, final, rel, expected)
            for (target, final, rel, expected) in planned
            if _read_existing(target) != final
        ]
    except OSError as exc:
        return {"ok": False, "result": f"unsafe or unreadable vault target: {exc}"}
    if not to_write:
        return {"ok": True, "result": "unchanged (idempotent no-op)"}

    temps: list[tuple[Path, Path, str, str | None]] = []
    for target, final, rel, expected in to_write:
        try:
            tmp_path = stage_text(target, final)
        except OSError as e:
            _cleanup_temps([t for (t, _tg, _r, _expected) in temps])
            return retryable_failure(f"vault cascade staging failed at {rel}: {e}")
        temps.append((tmp_path, target, rel, expected))

    applied: list[str] = []
    for idx, (tmp_path, target, rel, expected) in enumerate(temps):
        try:
            commit_staged_if_sha256(tmp_path, target, expected)
        except (OSError, StaleWriteError) as e:
            _cleanup_temps([t for (t, _tg, _r, _expected) in temps[idx:]])
            if not applied:
                return retryable_failure(f"vault cascade became stale at {rel}: {e}")
            store.log_event(
                actor="cascade",
                action="vault_cascade_partial",
                subject=(payload.get("summary") or "").strip() or "vault cascade",
                trust="trusted",
                detail=f"PARTIAL apply: applied={applied}; failed at {rel}: {e}",
            )
            return {
                "ok": False,
                "result": (
                    f"CRITICAL partial cascade: applied {applied} then failed at {rel}: {e} — "
                    "vault may be inconsistent; inspect the applied files"
                ),
            }
        applied.append(rel)

    summary = (payload.get("summary") or "").strip()
    action_id = (payload.get("_action_id") or "").strip()
    detail = "files: " + ", ".join(applied)
    if action_id:
        detail += f" (action {action_id})"
    store.log_event(
        actor="cascade",
        action="vault_files_edited",
        subject=summary or f"vault cascade ({len(applied)} files)",
        trust="trusted",
        payload=summary,
        detail=detail,
    )
    return {"ok": True, "result": f"applied {len(applied)} file(s): {', '.join(applied)}"}
