from __future__ import annotations

import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.brain.connector.layout import VaultLayout, load_layout, owner_only_paths

THURSDAY = datetime(2026, 10, 22, 9, 30, tzinfo=ZoneInfo("Europe/Berlin"))


def _profile(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_match_today(tmp_path: Path) -> None:
    layout = load_layout(tmp_path / "missing.yaml")

    assert layout == VaultLayout()
    assert layout.daily_notes_dir == "01 - Daily Notes"
    assert layout.inbox_dir == "00 - Inbox"
    assert layout.templates_dir == "11 - Templates"
    assert layout.actions_dir == "Actions"
    assert layout.daily_note_template == "Daily Note.md"
    assert layout.daily_note_path(THURSDAY) == "01 - Daily Notes/2026/10/2026-10-22.md"
    assert layout.template_path == "11 - Templates/Daily Note.md"


def test_no_path_gives_defaults() -> None:
    assert load_layout(None) == VaultLayout()


def test_profile_without_notes_gives_defaults(tmp_path: Path) -> None:
    assert load_layout(_profile(tmp_path, "user:\n  name: Alex\n")) == VaultLayout()


def test_empty_profile_gives_defaults(tmp_path: Path) -> None:
    assert load_layout(_profile(tmp_path, "")) == VaultLayout()


def test_profile_overrides_layout(tmp_path: Path) -> None:
    profile = _profile(
        tmp_path,
        "notes:\n"
        "  folder_name: my notes\n"
        "  daily_notes_dir: Journal\n"
        "  templates_dir: Templates\n"
        "  actions_dir: Open\n"
        "  daily_note_template: Day.md\n",
    )

    layout = load_layout(profile)

    assert layout.daily_notes_dir == "Journal"
    assert layout.templates_dir == "Templates"
    assert layout.actions_dir == "Open"
    assert layout.daily_note_template == "Day.md"
    assert layout.inbox_dir == "00 - Inbox"
    assert layout.daily_note_path(THURSDAY) == "Journal/2026/10/2026-10-22.md"
    assert layout.template_path == "Templates/Day.md"


@pytest.mark.parametrize("value", ['"../x"', '"a/../b"', '"/abs"', '""', '"   "', "5"])
def test_bad_layout_value_raises(tmp_path: Path, value: str) -> None:
    profile = _profile(tmp_path, f"notes:\n  daily_notes_dir: {value}\n")

    with pytest.raises(ValueError, match="daily_notes_dir"):
        load_layout(profile)


def test_owner_only_paths_follow_the_daily_notes_dir() -> None:
    assert owner_only_paths(VaultLayout()) == ("01 - Daily Notes/Action Queue.md",)
    assert owner_only_paths(VaultLayout(daily_notes_dir="Journal")) == ("Journal/Action Queue.md",)


NFD_DIR = unicodedata.normalize("NFD", "Tägliche Notizen")
NFC_DIR = unicodedata.normalize("NFC", "Tägliche Notizen")


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("Journal/", "Journal"),
        ("./Journal", "Journal"),
        ("Journal//", "Journal"),
        ("Work//Journal/", "Work/Journal"),
        (NFD_DIR, NFC_DIR),
    ],
)
def test_values_are_normalised(tmp_path: Path, typed: str, canonical: str) -> None:
    layout = load_layout(_profile(tmp_path, f'notes:\n  daily_notes_dir: "{typed}"\n'))

    assert layout.daily_notes_dir == canonical
    assert owner_only_paths(layout) == (f"{canonical}/Action Queue.md",)


@pytest.mark.parametrize("value", [".", "./", "a/..", "//"])
def test_value_that_names_nothing_raises(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="actions_dir"):
        load_layout(_profile(tmp_path, f'notes:\n  actions_dir: "{value}"\n'))
