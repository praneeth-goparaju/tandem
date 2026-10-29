from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import pytest

from src.brain.connector.layout import VaultLayout, load_layout, owner_only_paths
from src.brain.connector.vault.paths import (
    OWNER_ONLY,
    PROTECTED,
    PathRefused,
    check_writable,
    resolve_in_vault,
)

NFC_NAME = unicodedata.normalize("NFC", "Führerschein.md")
NFD_NAME = unicodedata.normalize("NFD", "Führerschein.md")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    root.mkdir()
    (root / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    (root / "01 - Daily Notes").mkdir()
    (root / "01 - Daily Notes" / "Action Queue.md").write_text("queue\n", encoding="utf-8")
    (root / "Notes").mkdir()
    (root / "Notes" / "a.md").write_text("a\n", encoding="utf-8")
    return root


def test_protected_lists_match_the_rules() -> None:
    assert PROTECTED == (
        "CLAUDE.md",
        "Instructions/",
        "Brain Agents/",
        ".claude/",
        ".obsidian/",
        ".trash/",
    )
    assert OWNER_ONLY == ("01 - Daily Notes/Action Queue.md",)
    assert OWNER_ONLY == owner_only_paths(VaultLayout())


def test_owner_only_follows_the_layout(vault: Path) -> None:
    owner_only = owner_only_paths(VaultLayout(daily_notes_dir="Journal"))

    with pytest.raises(PathRefused, match="Action Queue.md is owner-only"):
        check_writable(vault, "Journal/Action Queue.md", owner_only=owner_only)
    assert (
        check_writable(vault, "01 - Daily Notes/Action Queue.md", owner_only=owner_only).name
        == "Action Queue.md"
    )


@pytest.mark.parametrize("typed", ["Journal/", "./Journal", "Journal//"])
def test_owner_only_holds_for_a_value_typed_loosely(
    vault: Path, tmp_path: Path, typed: str
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(f'notes:\n  daily_notes_dir: "{typed}"\n', encoding="utf-8")
    owner_only = owner_only_paths(load_layout(profile))

    with pytest.raises(PathRefused, match="Action Queue.md is owner-only"):
        check_writable(vault, "Journal/Action Queue.md", owner_only=owner_only)


def test_inside_vault_resolves(vault: Path) -> None:
    resolved = resolve_in_vault(vault, "Notes/a.md")
    assert resolved.samefile(vault / "Notes" / "a.md")
    assert check_writable(vault, "Notes/a.md").samefile(vault / "Notes" / "a.md")


def test_new_file_inside_vault_is_writable(vault: Path) -> None:
    resolved = check_writable(vault, "Notes/new note.md")
    assert resolved.parent.samefile(vault / "Notes")
    assert resolved.name == "new note.md"


def test_outside_vault_refused(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (vault / "Notes" / "link.md").symlink_to(outside)
    (vault / "Notes" / "linked dir").symlink_to(tmp_path, target_is_directory=True)

    for rel in (
        "../x.md",
        "Notes/../../x.md",
        "/etc/hosts",
        "Notes/link.md",
        "Notes/linked dir/outside.md",
    ):
        with pytest.raises(PathRefused, match="outside the vault"):
            resolve_in_vault(vault, rel)
        with pytest.raises(PathRefused, match="outside the vault"):
            check_writable(vault, rel)


def test_symlinked_vault_root_still_resolves(vault: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(vault, target_is_directory=True)
    assert resolve_in_vault(alias, "Notes/a.md").samefile(vault / "Notes" / "a.md")


def test_protected_refused_for_write(vault: Path) -> None:
    cases = {
        "CLAUDE.md": "CLAUDE.md is protected",
        "Instructions/a.md": "Instructions/ is protected",
        "Brain Agents/b.md": "Brain Agents/ is protected",
        ".claude/settings.json": ".claude/ is protected",
        ".obsidian/app.json": ".obsidian/ is protected",
        ".trash/old.md": ".trash/ is protected",
        "Notes/../CLAUDE.md": "CLAUDE.md is protected",
        "01 - Daily Notes/Action Queue.md": "Action Queue.md is owner-only",
    }
    for rel, message in cases.items():
        with pytest.raises(PathRefused) as refused:
            check_writable(vault, rel)
        assert str(refused.value) == message


def test_symlink_to_protected_refused_for_write(vault: Path) -> None:
    (vault / "Notes" / "rules.md").symlink_to(vault / "CLAUDE.md")
    with pytest.raises(PathRefused, match="CLAUDE.md is protected"):
        check_writable(vault, "Notes/rules.md")


def test_protected_readable(vault: Path) -> None:
    assert resolve_in_vault(vault, "CLAUDE.md").samefile(vault / "CLAUDE.md")
    queue = vault / "01 - Daily Notes" / "Action Queue.md"
    assert resolve_in_vault(vault, "01 - Daily Notes/Action Queue.md").samefile(queue)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="relies on APFS treating NFC and NFD names as the same file",
)
def test_unicode_nfd_matches_nfc(vault: Path) -> None:
    on_disk = vault / "Notes" / NFD_NAME
    on_disk.write_text("licence\n", encoding="utf-8")
    assert NFC_NAME != NFD_NAME

    resolved = resolve_in_vault(vault, f"Notes/{NFC_NAME}")
    assert resolved.exists()
    assert resolved.samefile(on_disk)
    assert check_writable(vault, f"Notes/{NFC_NAME}").samefile(on_disk)
    assert len(os.listdir(vault / "Notes")) == 2


def test_unicode_nfd_protected_name_refused(vault: Path) -> None:
    with pytest.raises(PathRefused, match="Action Queue.md is owner-only"):
        check_writable(vault, unicodedata.normalize("NFD", "01 - Daily Notes/Action Queue.md"))


def test_case_variants_of_protected(vault: Path) -> None:
    for rel, message in (
        ("claude.md", "CLAUDE.md is protected"),
        ("INSTRUCTIONS/a.md", "Instructions/ is protected"),
        (".Obsidian/app.json", ".obsidian/ is protected"),
        ("01 - daily notes/action queue.md", "Action Queue.md is owner-only"),
    ):
        with pytest.raises(PathRefused) as refused:
            check_writable(vault, rel)
        assert str(refused.value) == message


PLANTED = (
    "CLAUDE.local.md",
    "AGENTS.md",
    "Notes/CLAUDE.md",
    "Notes/AGENTS.md",
    "Notes/.claude/skills/x/SKILL.md",
    ".codex/config.toml",
    ".mcp.json",
    "Notes/.hidden.md",
    "Notes/Instructions/a.md",
    "Projects/Brain Agents/b.md",
    "Notes/agents.MD",
    "Notes/.Codex/config.toml",
)


def test_instruction_and_hidden_files_refused_anywhere(vault: Path) -> None:
    for rel in PLANTED:
        with pytest.raises(PathRefused, match="protected"):
            check_writable(vault, rel)


def test_refusal_names_the_rule(vault: Path) -> None:
    for rel in ("AGENTS.md", "Notes/CLAUDE.md", ".mcp.json", "Notes/Instructions/a.md"):
        with pytest.raises(PathRefused, match="instruction files are protected"):
            check_writable(vault, rel)
    with pytest.raises(PathRefused, match="hidden files and folders are protected"):
        check_writable(vault, "Notes/.hidden.md")


def test_symlink_to_a_nested_instruction_file_refused(vault: Path) -> None:
    (vault / "Notes" / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (vault / "Notes" / "plain.md").symlink_to(vault / "Notes" / "AGENTS.md")
    with pytest.raises(PathRefused, match="instruction files are protected"):
        check_writable(vault, "Notes/plain.md")


def test_instruction_and_hidden_files_stay_readable(vault: Path) -> None:
    for rel in PLANTED:
        assert resolve_in_vault(vault, rel) == (vault / rel).resolve()


def test_ordinary_names_with_dots_stay_writable(vault: Path) -> None:
    for rel in ("Notes/v1.2 plan.md", "Notes/Mr. Smith.md", "Notes/claude notes.md"):
        assert check_writable(vault, rel).name == PurePosixPath(rel).name
