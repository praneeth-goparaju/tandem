from __future__ import annotations

import hashlib
import os
import re
from datetime import date
from pathlib import Path

from src.brain.atomic_io import atomic_create_text, confined_path, read_regular_text
from src.db import get_db
from src.memory import store

_SLUG_MAX = 60
_IDENTITY_MAX = 24


class PromotionConflict(RuntimeError):
    ...


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-") or "note"


def stable_identity(value: str, body: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if cleaned:
        return cleaned[:_IDENTITY_MAX].rstrip("-")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def promotion_path(
    *,
    vault_root: Path,
    configured_notes: Path,
    date_str: str,
    title: str,
    identity: str,
    body: str,
) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise ValueError("invalid promotion date")
    date.fromisoformat(date_str)
    vault_root = Path(vault_root).expanduser()
    configured_notes = Path(configured_notes).expanduser()
    if not configured_notes.is_absolute():
        configured_notes = vault_root / configured_notes
    notes_relative = Path(os.path.abspath(configured_notes)).relative_to(
        Path(os.path.abspath(vault_root))
    )
    notes_dir = confined_path(vault_root, notes_relative)
    suffix = stable_identity(identity, body)
    path = notes_dir / f"{date_str}-{slugify(title)}-{suffix}.md"
    return vault_root.resolve(), path


def publish_once(path: Path, body: str) -> bool:
    try:
        atomic_create_text(path, body)
        return True
    except FileExistsError as conflict:
        try:
            existing = read_regular_text(path)
        except OSError as exc:
            raise PromotionConflict(
                f"promotion target exists but is not a safe regular file: {path.name}"
            ) from exc
        if existing != body:
            raise PromotionConflict(
                f"promotion target already exists with different content: {path.name}"
            ) from conflict
        return False


def log_event_once(
    *,
    actor: str,
    action: str,
    subject: str,
    trust: str,
    payload: str,
    detail: str,
) -> None:
    exists = get_db().execute(
        "SELECT 1 FROM events WHERE action=? AND detail=? LIMIT 1",
        (action, detail),
    ).fetchone()
    if exists is None:
        store.log_event(
            actor=actor,
            action=action,
            subject=subject,
            trust=trust,
            payload=payload,
            detail=detail,
        )
