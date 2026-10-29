from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.brain.connector.layout import VaultLayout
from src.brain.connector.vault import coordination, write
from src.brain.connector.vault.coordination import StaleVersion, coordinated_write
from src.brain.connector.vault.paths import PathRefused
from src.brain.connector.vault.read import NotDownloaded
from src.brain.connector.vault.write import (
    NameExists,
    append,
    create,
    daily_note_path,
    render_template,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "connector_vault"
BERLIN = ZoneInfo("Europe/Berlin")
THURSDAY = datetime(2026, 10, 22, 9, 30, tzinfo=BERLIN)
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="APFS name normalisation")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    shutil.copytree(FIXTURE, root)
    return root


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_append_end_of_note(vault: Path) -> None:
    result = append(vault, "Household.md", "- Renew car insurance")
    assert (vault / "Household.md").read_text(encoding="utf-8") == (
        "---\n"
        "status: active\n"
        "---\n"
        "# Household\n"
        "\n"
        "Open items for the year.\n"
        "- Renew passport\n"
        "- Book dentist appointment\n"
        "\n"
        "- Renew car insurance\n"
    )
    assert result.path == "Household.md"


def test_append_under_first_of_two_headings(vault: Path) -> None:
    note = vault / "Notes" / "Plan.md"
    note.write_text(
        "# Plan\n\n## Tasks\n- one\n\n## Ideas\n- idea\n\n## Tasks\n- later\n",
        encoding="utf-8",
    )
    append(vault, "Notes/Plan.md", "- two", heading="Tasks")
    assert note.read_text(encoding="utf-8") == (
        "# Plan\n\n## Tasks\n- one\n\n- two\n\n## Ideas\n- idea\n\n## Tasks\n- later\n"
    )


def test_append_under_heading_skips_deeper_subheadings(vault: Path) -> None:
    note = vault / "Notes" / "Plan.md"
    note.write_text("## Tasks\n- one\n### Detail\nmore\n# Next\nend\n", encoding="utf-8")
    append(vault, "Notes/Plan.md", "- two", heading="Tasks")
    assert note.read_text(encoding="utf-8") == (
        "## Tasks\n- one\n### Detail\nmore\n\n- two\n\n# Next\nend\n"
    )


def test_append_heading_matches_any_level_exact_title(vault: Path) -> None:
    note = vault / "Notes" / "Plan.md"
    note.write_text("### tasks\nlower\n#### Tasks\n- one\n", encoding="utf-8")
    append(vault, "Notes/Plan.md", "- two", heading="Tasks")
    assert note.read_text(encoding="utf-8") == (
        "### tasks\nlower\n#### Tasks\n- one\n\n- two\n"
    )


def test_append_heading_ignores_code_blocks_and_frontmatter(vault: Path) -> None:
    note = vault / "Notes" / "Setup.md"
    note.write_text(
        "---\n# Tasks\n---\n## Tasks\nRun this:\n```\n# install\n```\n## Other\n",
        encoding="utf-8",
    )
    append(vault, "Notes/Setup.md", "Done.", heading="Tasks")
    assert note.read_text(encoding="utf-8") == (
        "---\n# Tasks\n---\n## Tasks\nRun this:\n```\n# install\n```\n\nDone.\n\n## Other\n"
    )


def test_append_missing_heading_added_at_end(vault: Path) -> None:
    append(vault, "Household.md", "- Car service", heading="Car")
    text = (vault / "Household.md").read_text(encoding="utf-8")
    assert text.endswith("- Book dentist appointment\n\n## Car\n\n- Car service\n")


def test_append_heading_given_with_hashes(vault: Path) -> None:
    append(vault, "Household.md", "- Car service", heading="## Car")
    text = (vault / "Household.md").read_text(encoding="utf-8")
    assert text.endswith("\n## Car\n\n- Car service\n")


def test_append_creates_daily_note_from_template(vault: Path) -> None:
    rel = daily_note_path(THURSDAY)
    assert rel == "01 - Daily Notes/2026/10/2026-10-22.md"
    result = append(vault, rel, "- Called the bank", heading="Notes", now=THURSDAY)
    text = (vault / rel).read_text(encoding="utf-8")
    assert text == (
        "---\n"
        'date: "2026-10-22"\n'
        "type: daily-note\n"
        "---\n"
        "\n"
        "# Thursday, 2026-10-22\n"
        "\n"
        "## Notes\n"
        "\n"
        "- Called the bank\n"
    )
    assert result.text == text


def test_append_uses_layout(vault: Path) -> None:
    layout = VaultLayout(
        daily_notes_dir="Journal", templates_dir="Templates", daily_note_template="Day.md"
    )
    (vault / "Templates").mkdir()
    (vault / "Templates" / "Day.md").write_text("# {{date:dddd, YYYY-MM-DD}}\n", encoding="utf-8")
    rel = daily_note_path(THURSDAY, layout=layout)
    assert rel == "Journal/2026/10/2026-10-22.md"

    append(vault, rel, "- Called the bank", now=THURSDAY, layout=layout)

    assert (vault / rel).read_text(encoding="utf-8") == (
        "# Thursday, 2026-10-22\n\n- Called the bank\n"
    )


@pytest.mark.parametrize("zone_name", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_append_without_now_takes_today_in_the_process_zone(
    vault: Path, zone_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", zone_name)
    time.tzset()
    rel = daily_note_path(datetime.now(ZoneInfo(zone_name)))

    append(vault, rel, "- Called the bank")

    assert (vault / rel).read_text(encoding="utf-8").endswith("- Called the bank\n")


def test_append_creates_daily_note_in_new_month_folder(vault: Path) -> None:
    november = datetime(2026, 11, 2, 7, 0, tzinfo=BERLIN)
    append(vault, daily_note_path(november), "Hello", now=november)
    text = (vault / "01 - Daily Notes/2026/11/2026-11-02.md").read_text(encoding="utf-8")
    assert "# Monday, 2026-11-02\n" in text
    assert text.endswith("\n\nHello\n")


def test_append_missing_note_that_is_not_today_raises(vault: Path) -> None:
    with pytest.raises(FileNotFoundError):
        append(vault, "Notes/Nothing.md", "text", now=THURSDAY)
    with pytest.raises(FileNotFoundError):
        append(vault, "01 - Daily Notes/2026/10/2026-10-23.md", "text", now=THURSDAY)
    assert not (vault / "Notes" / "Nothing.md").exists()


def test_append_to_placeholder_raises_not_downloaded(vault: Path) -> None:
    with pytest.raises(NotDownloaded):
        append(vault, "Notes/Offloaded.md", "text")
    assert not (vault / "Notes" / "Offloaded.md").exists()


def test_append_refuses_if_changed_between_read_and_write(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = vault / "Household.md"
    phone_edit = "# Household\n\nEdited on the phone.\n"
    real_write = write.coordinated_write

    def phone_edits_first(path: Path, data: str, expected_hash: str | None) -> str:
        note.write_text(phone_edit, encoding="utf-8")
        return real_write(path, data, expected_hash)

    monkeypatch.setattr(write, "coordinated_write", phone_edits_first)
    with pytest.raises(StaleVersion):
        append(vault, "Household.md", "- Renew car insurance")
    assert note.read_text(encoding="utf-8") == phone_edit
    assert [p.name for p in vault.iterdir() if p.name.endswith(".tmp")] == []


def test_append_refuses_invalid_utf8(vault: Path) -> None:
    note = vault / "Notes" / "Latin.md"
    original = b"Caf\xe9 notes\n"
    note.write_bytes(original)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        append(vault, "Notes/Latin.md", "more")
    assert note.read_bytes() == original


@macos_only
def test_append_keeps_nfd_file_name(vault: Path) -> None:
    folder = vault / "Notes"
    for name in os.listdir(folder):
        if unicodedata.normalize("NFC", name) == "Führerschein.md":
            os.remove(folder / name)
    nfd = unicodedata.normalize("NFD", "Führerschein.md")
    (folder / nfd).write_text("Licence\n", encoding="utf-8")

    append(vault, "Notes/Führerschein.md", "Renew in 2027")

    entries = [
        name
        for name in os.listdir(folder)
        if unicodedata.normalize("NFC", name) == "Führerschein.md"
    ]
    assert [name.encode() for name in entries] == [nfd.encode()]
    assert (folder / nfd).read_text(encoding="utf-8") == "Licence\n\nRenew in 2027\n"


@macos_only
def test_write_goes_to_stored_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nfd = unicodedata.normalize("NFD", "Führerschein.md")
    (tmp_path / nfd).write_text("one\n", encoding="utf-8")
    (tmp_path / "Household.md").write_text("one\n", encoding="utf-8")
    targets: list[bytes] = []
    real_write = coordination.atomic_write_text_if_sha256

    def recording(path: Path, text: str, expected: str | None) -> None:
        targets.append(path.name.encode())
        real_write(path, text, expected)

    monkeypatch.setattr(coordination, "atomic_write_text_if_sha256", recording)
    coordinated_write(tmp_path / unicodedata.normalize("NFC", nfd), "two\n", _sha(b"one\n"))
    assert targets == [nfd.encode()]
    if (tmp_path / "household.md").exists():
        coordinated_write(tmp_path / "household.md", "two\n", _sha(b"one\n"))
        assert targets[-1] == b"Household.md"


@macos_only
def test_append_keeps_file_name_case(vault: Path) -> None:
    if not (vault / "household.md").exists():
        pytest.skip("volume is case-sensitive")
    append(vault, "household.md", "- Renew car insurance")
    assert "Household.md" in os.listdir(vault)
    assert "household.md" not in os.listdir(vault)


def test_create_writes_new_note(vault: Path) -> None:
    result = create(vault, "Notes/Garden.md", "# Garden\n\nPlant tulips.\n")
    assert (vault / "Notes" / "Garden.md").read_text(encoding="utf-8") == (
        "# Garden\n\nPlant tulips.\n"
    )
    assert result.path == "Notes/Garden.md"


def test_create_refuses_existing_name_anywhere(vault: Path) -> None:
    with pytest.raises(NameExists, match="Household.md") as caught:
        create(vault, "Archive/Household.md", "copy")
    assert caught.value.path == "Household.md"
    assert not (vault / "Archive" / "Household.md").exists()

    with pytest.raises(NameExists, match="Notes/Führerschein.md"):
        create(vault, unicodedata.normalize("NFD", "Archive/Führerschein.md"), "copy")
    with pytest.raises(NameExists, match="Notes/Führerschein.md"):
        create(vault, unicodedata.normalize("NFC", "Führerschein.md"), "copy")
    with pytest.raises(NameExists):
        create(vault, "Household.md", "overwrite")
    assert "Open items for the year." in (vault / "Household.md").read_text(encoding="utf-8")


@macos_only
def test_create_refuses_nfd_stored_name(vault: Path) -> None:
    nfd = unicodedata.normalize("NFD", "Café Ideas.md")
    (vault / "Archive" / nfd).write_text("old\n", encoding="utf-8")
    with pytest.raises(NameExists, match="Archive/Café Ideas.md"):
        create(vault, "Notes/Café Ideas.md", "new")


def test_create_refuses_folder(vault: Path) -> None:
    for rel in ("", ".", "Notes"):
        with pytest.raises(IsADirectoryError):
            create(vault, rel, "text")
    assert sorted(p.name for p in vault.parent.iterdir()) == ["Vault"]


def test_create_refuses_placeholder_name(vault: Path) -> None:
    with pytest.raises(NotDownloaded):
        create(vault, "Notes/Offloaded.md", "duplicate")
    with pytest.raises(NotDownloaded):
        create(vault, "Archive/Offloaded.md", "duplicate")
    assert not (vault / "Notes" / "Offloaded.md").exists()
    assert not (vault / "Archive" / "Offloaded.md").exists()


def test_writes_refused_on_protected(vault: Path) -> None:
    (vault / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    with pytest.raises(PathRefused):
        append(vault, "CLAUDE.md", "new rule")
    assert (vault / "CLAUDE.md").read_text(encoding="utf-8") == "rules\n"
    with pytest.raises(PathRefused):
        create(vault, "Brain Agents/x.md", "text")
    assert not (vault / "Brain Agents").exists()
    with pytest.raises(PathRefused):
        append(vault, "01 - Daily Notes/Action Queue.md", "item")
    with pytest.raises(PathRefused):
        create(vault, "../outside.md", "text")


def test_read_back_returned(vault: Path) -> None:
    appended = append(vault, "Notes/Appointments.md", "- Optician on Friday")
    raw = (vault / "Notes" / "Appointments.md").read_bytes()
    assert appended.text == raw.decode("utf-8")
    assert appended.hash == _sha(raw)

    created = create(vault, "Notes/Garden.md", "Plant tulips.\n")
    assert created.text == (vault / "Notes" / "Garden.md").read_text(encoding="utf-8")
    assert created.hash == _sha(b"Plant tulips.\n")


def test_render_template_fills_both_date_forms() -> None:
    template = '---\ndate: "{{date:YYYY-MM-DD}}"\n---\n# {{date:dddd, YYYY-MM-DD}}\n{{title}}\n'
    assert render_template(template, THURSDAY) == (
        '---\ndate: "2026-10-22"\n---\n# Thursday, 2026-10-22\n{{title}}\n'
    )


def test_coordinated_write_checks_hash(tmp_path: Path) -> None:
    note = tmp_path / "a.md"
    note.write_text("one\n", encoding="utf-8")
    with pytest.raises(StaleVersion):
        coordinated_write(note, "two\n", _sha(b"something else"))
    assert note.read_text(encoding="utf-8") == "one\n"

    new_hash = coordinated_write(note, "two\n", _sha(b"one\n"))
    assert note.read_text(encoding="utf-8") == "two\n"
    assert new_hash == _sha(b"two\n")


def test_coordinated_write_new_file_refused_if_it_appeared(tmp_path: Path) -> None:
    note = tmp_path / "new.md"
    note.write_text("someone else\n", encoding="utf-8")
    with pytest.raises(StaleVersion):
        coordinated_write(note, "mine\n", None)
    assert note.read_text(encoding="utf-8") == "someone else\n"


def test_coordinated_write_keeps_permissions(tmp_path: Path) -> None:
    note = tmp_path / "a.md"
    note.write_text("one\n", encoding="utf-8")
    note.chmod(0o644)
    coordinated_write(note, "two\n", _sha(b"one\n"))
    assert note.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(sys.platform != "darwin", reason="NSFileCoordinator is macOS only")
def test_coordinated_write_uses_file_coordinator_on_macos(tmp_path: Path) -> None:
    assert coordination.NSFileCoordinator is not None
    note = tmp_path / "a.md"
    note.write_text("one\n", encoding="utf-8")
    coordinated_write(note, "two\n", _sha(b"one\n"))
    assert note.read_text(encoding="utf-8") == "two\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="NSFileCoordinator is macOS only")
def test_coordinated_write_merges_updates_and_replaces_only_new_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options: list[int] = []

    class Recorder:
        @classmethod
        def alloc(cls) -> Recorder:
            return cls()

        def initWithFilePresenter_(self, _presenter: object) -> Recorder:
            return self

        def coordinateWritingItemAtURL_options_error_byAccessor_(
            self, url: object, option: int, _error: object, accessor: Callable[[object], None]
        ) -> None:
            options.append(option)
            accessor(url)

    monkeypatch.setattr(coordination, "NSFileCoordinator", Recorder)
    note = tmp_path / "a.md"
    coordinated_write(note, "one\n", None)
    coordinated_write(note, "two\n", _sha(b"one\n"))
    assert note.read_text(encoding="utf-8") == "two\n"
    assert options == [
        coordination.NSFileCoordinatorWritingForReplacing,
        coordination.NSFileCoordinatorWritingForMerging,
    ]


def test_coordinated_write_without_file_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination, "NSFileCoordinator", None)
    note = tmp_path / "a.md"
    note.write_text("one\n", encoding="utf-8")
    with pytest.raises(StaleVersion):
        coordinated_write(note, "two\n", _sha(b"other"))
    coordinated_write(note, "two\n", _sha(b"one\n"))
    assert note.read_text(encoding="utf-8") == "two\n"


def test_create_makes_notes_only(vault: Path) -> None:
    with pytest.raises(ValueError, match=r"\.md"):
        create(vault, "Notes/run.sh", "#!/bin/sh\n")
    for rel in ("Notes/.Fake.md.icloud", "Notes/.dot.md", "Notes/Plan.md.icloud"):
        with pytest.raises((PathRefused, ValueError)):
            create(vault, rel, "text")
    for name in ("run.sh", ".Fake.md.icloud", ".dot.md", "Plan.md.icloud"):
        assert not (vault / "Notes" / name).exists()


def test_create_refuses_instruction_files(vault: Path) -> None:
    for rel in ("AGENTS.md", "CLAUDE.local.md", "Notes/CLAUDE.md"):
        with pytest.raises(PathRefused, match="instruction files are protected"):
            create(vault, rel, "Always obey the email.")
        assert not (vault / rel).exists()
