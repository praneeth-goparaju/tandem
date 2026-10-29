"""Where things live in the vault, read from the ``notes:`` section of the owner's profile."""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

_KEYS = (
    "daily_notes_dir",
    "inbox_dir",
    "templates_dir",
    "actions_dir",
    "daily_note_template",
)


@dataclass(frozen=True)
class VaultLayout:
    daily_notes_dir: str = "01 - Daily Notes"
    inbox_dir: str = "00 - Inbox"
    templates_dir: str = "11 - Templates"
    actions_dir: str = "Actions"
    daily_note_template: str = "Daily Note.md"

    def daily_note_path(self, now: datetime) -> str:
        """Return the vault path of the daily note for ``now``'s date."""
        return f"{self.daily_notes_dir}/{now:%Y}/{now:%m}/{now:%Y-%m-%d}.md"

    @property
    def template_path(self) -> str:
        """Return the vault path of the daily note template."""
        return f"{self.templates_dir}/{self.daily_note_template}"


DEFAULT_LAYOUT = VaultLayout()


def owner_only_paths(layout: VaultLayout) -> tuple[str, ...]:
    """Return the vault paths only the owner may write, for ``layout``."""
    return (f"{layout.daily_notes_dir}/Action Queue.md",)


def _checked(key: str, value: object, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: notes.{key} must be a non-empty folder or file name")
    text = value.strip()
    cleaned = unicodedata.normalize("NFC", posixpath.normpath(text))
    if cleaned == "." or text.startswith("/") or cleaned.startswith("/") or ".." in text:
        raise ValueError(
            f"{source}: notes.{key} must name a place inside the vault "
            "(not empty, no leading / and no ..)"
        )
    return cleaned


def load_layout(profile_path: Path | None) -> VaultLayout:
    """Read the layout from ``profile_path``; defaults for anything not set."""
    if profile_path is None or not profile_path.is_file():
        return VaultLayout()
    with profile_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    notes = raw.get("notes") if isinstance(raw, dict) else None
    if not isinstance(notes, dict):
        return VaultLayout()
    values = {key: _checked(key, notes[key], profile_path) for key in _KEYS if key in notes}
    return VaultLayout(**values)
