from __future__ import annotations

import hashlib
import os
import shutil
import unicodedata
from pathlib import Path

import pytest

from src.brain.connector.vault.paths import PathRefused
from src.brain.connector.vault.read import (
    Match,
    NotDownloaded,
    list_folder,
    read_note,
    search,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "connector_vault"
DAILY = "01 - Daily Notes/2026/10/2026-10-21.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    shutil.copytree(FIXTURE, root)
    return root


def test_search_finds_line_with_number(vault: Path) -> None:
    matches = search(vault, "dentist")
    assert Match(path=DAILY, line=7, text="- Call the dentist about the appointment.") in matches
    assert Match(path="Household.md", line=8, text="- Book dentist appointment") in matches


def test_search_case_insensitive_and_unicode(vault: Path) -> None:
    assert search(vault, "FÜHRERSCHEIN", folder="Notes")
    assert search(vault, "führerschein", folder="Notes")
    assert search(vault, "fuhrerschein") == []
    nfd = unicodedata.normalize("NFD", "führerschein")
    assert search(vault, nfd, folder="Notes")


def test_search_finds_note_stored_with_nfd_name_and_nfd_text(vault: Path) -> None:
    nfd_name = unicodedata.normalize("NFD", "Straße Übersicht.md")
    body = unicodedata.normalize("NFD", "Führerschein verlängern\n")
    (vault / "Notes" / nfd_name).write_text(body, encoding="utf-8")
    matches = search(vault, "führerschein verlängern")
    assert [(m.path, m.line) for m in matches] == [("Notes/Straße Übersicht.md", 1)]
    assert matches[0].path == unicodedata.normalize("NFC", matches[0].path)
    assert matches[0].text == unicodedata.normalize("NFC", matches[0].text)


def test_search_on_disk_name_is_used_when_names_are_not_normalised(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nfd_name = unicodedata.normalize("NFD", "Straße Übersicht.md")
    (vault / "Notes" / nfd_name).write_text("Führerschein\n", encoding="utf-8")
    original = Path.read_bytes

    def exact_name_only(self: Path) -> bytes:
        if self.name not in os.listdir(self.parent):
            raise FileNotFoundError(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", exact_name_only)
    paths = {m.path for m in search(vault, "führerschein", folder="Notes")}
    assert "Notes/Straße Übersicht.md" in paths


def test_search_frontmatter_field(vault: Path) -> None:
    matches = search(vault, "dentist", field=("status", "active"))
    assert {m.path for m in matches} == {"Household.md"}


def test_search_field_skips_broken_yaml(vault: Path) -> None:
    matches = search(vault, "appointment", field=("status", "active"))
    paths = {m.path for m in matches}
    assert "Household.md" in paths
    assert "Notes/Broken Frontmatter.md" not in paths


def test_search_field_survives_impossible_date(vault: Path) -> None:
    matches = search(vault, "hello", field=("status", "active"))
    assert matches == []
    matches = search(vault, "dentist", field=("status", "active"))
    assert {m.path for m in matches} == {"Household.md"}
    assert search(vault, "hello", folder="Notes")


def test_search_without_field_still_reads_broken_yaml_note(vault: Path) -> None:
    paths = {m.path for m in search(vault, "appointment", limit=500)}
    assert "Notes/Broken Frontmatter.md" in paths


def test_search_skips_hidden_dirs(vault: Path) -> None:
    paths = {m.path for m in search(vault, "appointment", limit=500)}
    assert "Archive/Old Plan.md" in paths
    assert not any(part.startswith(".") for p in paths for part in p.split("/"))


def test_search_skips_icloud_placeholders(vault: Path) -> None:
    assert search(vault, "placeholder") == []


def test_search_limit(vault: Path) -> None:
    assert len(search(vault, "appointment", limit=3)) == 3


def test_search_folder_restricts(vault: Path) -> None:
    paths = {m.path for m in search(vault, "appointment", folder="Archive")}
    assert paths == {"Archive/Old Plan.md"}


@pytest.mark.parametrize("folder", [".trash", "Notes/../.trash", ".obsidian", "Notes/../.claude"])
def test_search_named_dot_folder_returns_nothing(vault: Path, folder: str) -> None:
    assert search(vault, "appointment", folder=folder) == []


def test_search_empty_query_with_field_lists_matching_notes(vault: Path) -> None:
    matches = search(vault, "", field=("status", "active"))
    assert {m.path for m in matches} == {"Household.md"}
    life = next(m for m in matches if m.path == "Household.md")
    assert life == Match(path="Household.md", line=1, text="---")
    assert len(search(vault, "", field=("status", "active"), limit=1)) == 1


def test_search_empty_query_without_field_returns_nothing(vault: Path) -> None:
    assert search(vault, "") == []


def test_search_folder_outside_vault_refused(vault: Path) -> None:
    with pytest.raises(PathRefused):
        search(vault, "x", folder="../")


def test_search_missing_folder_returns_nothing(vault: Path) -> None:
    assert search(vault, "appointment", folder="No Such Folder") == []


def test_read_whole_note(vault: Path) -> None:
    result = read_note(vault, "Household.md")
    raw = (vault / "Household.md").read_bytes()
    assert result.path == "Household.md"
    assert result.text == raw.decode("utf-8")
    assert result.hash == hashlib.sha256(raw).hexdigest()
    assert result.total_lines == 8


def test_read_range_keeps_full_hash(vault: Path) -> None:
    result = read_note(vault, "Household.md", start=7, end=8)
    raw = (vault / "Household.md").read_bytes()
    assert result.text == "- Renew passport\n- Book dentist appointment\n"
    assert result.hash == hashlib.sha256(raw).hexdigest()
    assert result.total_lines == 8


def test_read_range_open_ended_and_clamped(vault: Path) -> None:
    assert read_note(vault, "Household.md", start=7).text == (
        "- Renew passport\n- Book dentist appointment\n"
    )
    assert read_note(vault, "Household.md", end=1).text == "---\n"
    assert (
        read_note(vault, "Household.md", start=8, end=500).text == "- Book dentist appointment\n"
    )
    assert read_note(vault, "Household.md", start=50).text == ""


@pytest.mark.parametrize(("start", "end"), [(0, None), (-1, None), (None, 0), (None, -1), (1, -2)])
def test_read_bad_range_raises(vault: Path, start: int | None, end: int | None) -> None:
    with pytest.raises(ValueError):
        read_note(vault, "Household.md", start=start, end=end)


def test_read_placeholder_raises_not_downloaded(vault: Path) -> None:
    with pytest.raises(NotDownloaded):
        read_note(vault, "Notes/Offloaded.md")


def test_read_missing_file_raises_file_not_found(vault: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_note(vault, "Notes/Nothing.md")


def test_read_outside_vault_refused(vault: Path) -> None:
    with pytest.raises(PathRefused):
        read_note(vault, "../secret.md")


def test_read_protected_file_is_allowed(vault: Path) -> None:
    (vault / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    assert read_note(vault, "CLAUDE.md").text == "rules\n"


def test_list_folder_sorted_hides_dotfiles(vault: Path) -> None:
    assert list_folder(vault) == [
        "01 - Daily Notes/",
        "11 - Templates/",
        "Archive/",
        "Household.md",
        "Notes/",
    ]
    names = list_folder(vault, "Notes")
    assert names == sorted(names)
    assert "Offloaded.md.icloud" not in names
    assert ".Offloaded.md.icloud" not in names
    assert "Führerschein.md" in names


def test_list_folder_outside_vault_refused(vault: Path) -> None:
    with pytest.raises(PathRefused):
        list_folder(vault, "..")


def test_list_folder_missing_raises(vault: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list_folder(vault, "No Such Folder")
