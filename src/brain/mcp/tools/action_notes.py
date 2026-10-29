from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from src.brain.actions import task_links
from src.brain.atomic_io import (
    StaleWriteError,
    atomic_write_text_if_sha256,
    read_regular_bytes,
)
from src.brain.mcp._common import _err, _ok
from src.brain.mcp.caller_context import require_current_caller
from src.brain.mcp.tool_registry import brain_tool

_ACTION_ID_RE = re.compile(r"^A-\d{8}-\d{2}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_OPEN_STATUSES = frozenset({"next", "waiting", "scheduled"})
_TERMINAL_STATUSES = frozenset({"done", "cancelled"})
_IDENTITY_KEYS = ("type", "action_id")
_MAX_RATIONALE_LENGTH = 500
_COMPLETING_AGENT = "housekeeping"

log = logging.getLogger(__name__)


class _UniqueKeyLoader(yaml.SafeLoader):
    ...


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _today_in_configured_timezone() -> str:
    from src.orchestrator import config

    try:
        tz = ZoneInfo(config.TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid configured timezone: {config.TIMEZONE!r}") from exc
    return datetime.now(tz).date().isoformat()


def _validated_iso_date(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return text


def _split_frontmatter(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("action note has no YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], lines[index:]
    raise ValueError("action note has unterminated YAML frontmatter")


def _frontmatter_mapping(lines: list[str]) -> dict:
    try:
        parsed = yaml.load("".join(lines), Loader=_UniqueKeyLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid action-note frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("action-note frontmatter must be a mapping")
    return parsed


def _field_indexes(lines: list[str], key: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    return [index for index, line in enumerate(lines) if pattern.match(line)]


def _replace_scalar(lines: list[str], key: str, value: str, newline: str) -> None:
    indexes = _field_indexes(lines, key)
    if len(indexes) > 1:
        raise ValueError(f"action-note frontmatter has duplicate {key!r} fields")
    replacement = f"{key}: {value}{newline}"
    if indexes:
        lines[indexes[0]] = replacement
        return

    if key == "completed":
        updated = _field_indexes(lines, "updated")
        if len(updated) > 1:
            raise ValueError("action-note frontmatter has duplicate 'updated' fields")
        if updated:
            lines.insert(updated[0] + 1, replacement)
            return
    lines.append(replacement)


def _updated_note(
    original: str,
    *,
    action_id: str,
    completed_date: str,
    updated_date: str,
) -> str:
    frontmatter, untouched_tail = _split_frontmatter(original)
    metadata = _frontmatter_mapping(frontmatter)

    for key in (*_IDENTITY_KEYS, "status"):
        indexes = _field_indexes(frontmatter, key)
        if len(indexes) != 1:
            raise ValueError(
                f"action-note frontmatter requires one canonical {key!r} field"
            )
    for key in ("updated", "completed"):
        indexes = _field_indexes(frontmatter, key)
        if key in metadata and len(indexes) != 1:
            raise ValueError(
                f"action-note frontmatter requires canonical {key!r} spelling"
            )

    if metadata.get("type") != "action":
        raise ValueError("target note is not type: action")
    if str(metadata.get("action_id") or "") != action_id:
        raise ValueError("frontmatter action_id does not match the requested id")

    status_value = str(metadata.get("status") or "").strip().lower()
    if status_value in _TERMINAL_STATUSES:
        return original
    if status_value not in _OPEN_STATUSES:
        raise ValueError(f"unsupported action-note status: {status_value or '(missing)'}")

    newline = "\r\n" if original.startswith("---\r\n") else "\n"
    _replace_scalar(frontmatter, "status", "done", newline)
    _replace_scalar(frontmatter, "updated", updated_date, newline)
    _replace_scalar(frontmatter, "completed", completed_date, newline)
    return f"---{newline}{''.join(frontmatter)}{''.join(untouched_tail)}"


def _resolve_action_note(action_id: str) -> Path:
    from src.orchestrator import config

    vault_root = Path(config.VAULT_PATH).resolve()
    actions_entry = vault_root / "Actions"
    if actions_entry.is_symlink():
        raise ValueError("vault Actions directory cannot be a symlink")
    actions_dir = actions_entry.resolve()
    try:
        actions_dir.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError("vault Actions directory escapes the vault") from exc
    if not actions_dir.is_dir():
        raise ValueError("vault Actions directory does not exist")

    matches = sorted(actions_dir.glob(f"{action_id} - *.md"))
    if not matches:
        raise ValueError(f"no action note found for {action_id}")
    if len(matches) != 1:
        raise ValueError(f"multiple action notes found for {action_id}")

    target = matches[0]
    if target.is_symlink():
        raise ValueError("action-note symlinks are not supported")
    resolved = target.resolve()
    try:
        resolved.relative_to(actions_dir)
    except ValueError as exc:
        raise ValueError("action-note path escapes the Actions directory") from exc
    if not resolved.is_file():
        raise ValueError("action-note target is not a regular file")
    return resolved


def _read_text_exact(path: Path) -> str:
    return read_regular_bytes(path).decode("utf-8")


def _replace_atomic_if_unchanged(target: Path, original: str, updated: str) -> bool:
    if updated == original:
        return False
    try:
        atomic_write_text_if_sha256(
            target,
            updated,
            hashlib.sha256(original.encode()).hexdigest(),
        )
    except StaleWriteError as exc:
        if "busy" in str(exc):
            raise RuntimeError(
                "action note is busy; retry after the other edit finishes"
            ) from exc
        raise RuntimeError(
            "action note changed concurrently; retry after reading it again"
        ) from exc
    return True


@brain_tool(
    name="action_note_complete",
    description=(
        "Mark one existing vault Actions/A-*.md note done after a completed task "
        "settles its entire Outcome and every Step. This narrow tool only changes "
        "status, completed, and updated in frontmatter; it never changes the body. "
        "Read the whole note first. Do not call it for partial completion or doubt."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "pattern": r"^A-\d{8}-\d{2}$",
                "description": "Exact frontmatter action_id, e.g. A-20260819-01.",
            },
            "completed_date": {
                "type": "string",
                "format": "date",
                "description": "Task completion date in YYYY-MM-DD form.",
            },
            "task_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Exact completed backend task ID linked at creation.",
            },
            "settles_entire_outcome": {
                "type": "boolean",
                "description": "Must be true only after checking the whole Outcome and every Step.",
            },
            "rationale": {
                "type": "string",
                "maxLength": _MAX_RATIONALE_LENGTH,
                "description": "One sentence explaining why no step remains open.",
            },
        },
        "required": [
            "action_id",
            "task_id",
            "completed_date",
            "settles_entire_outcome",
            "rationale",
        ],
    },
)
async def action_note_complete(args):
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    if caller.agent != _COMPLETING_AGENT:
        return _err(f"only the {_COMPLETING_AGENT} run may complete action notes")
    action_id = str(args.get("action_id") or "").strip()
    if not _ACTION_ID_RE.fullmatch(action_id):
        return _err("action_id must match A-YYYYMMDD-NN")
    if args.get("settles_entire_outcome") is not True:
        return _err("refusing partial completion: settles_entire_outcome must be true")
    task_id = str(args.get("task_id") or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _err("task_id must be 1-64 letters, digits, or hyphens")
    if not task_links.is_completed_link(action_id, task_id):
        return _err(
            "task_id is not a durably linked, backend-verified completed task "
            f"for {action_id}"
        )
    rationale = str(args.get("rationale") or "").strip()
    if not rationale:
        return _err("rationale is required")
    if len(rationale) > _MAX_RATIONALE_LENGTH:
        return _err(f"rationale must be at most {_MAX_RATIONALE_LENGTH} characters")

    try:
        completed_date = _validated_iso_date(
            args.get("completed_date"), field="completed_date"
        )
        updated_date = _today_in_configured_timezone()
        if date.fromisoformat(completed_date) > date.fromisoformat(updated_date):
            raise ValueError("completed_date cannot be in the future")
        target = _resolve_action_note(action_id)
        original = _read_text_exact(target)
        updated = _updated_note(
            original,
            action_id=action_id,
            completed_date=completed_date,
            updated_date=updated_date,
        )
        changed = _replace_atomic_if_unchanged(target, original, updated)
    except (OSError, RuntimeError, ValueError) as exc:
        return _err(str(exc))

    if not changed:
        return _ok(f"Action note {action_id} already terminal; unchanged.")

    try:
        from src.memory import store

        store.log_event(
            actor=caller.agent,
            action="action_note_completed",
            subject=action_id,
            trust="trusted",
            payload=rationale,
            detail=(
                f"{target.name}; completed={completed_date}; "
                f"updated={updated_date}; task_id={task_id}; body=unchanged"
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to audit completed action note %s", action_id)

    return _ok(
        f"Completed action note {action_id}: status=done, "
        f"completed={completed_date}, updated={updated_date}; body unchanged."
    )
