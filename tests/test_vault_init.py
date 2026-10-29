from __future__ import annotations

import re
import shutil
import stat
import sys
from pathlib import Path

import pytest

from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout
from src.brain.vault_init import REPO_ROOT, init_vault

TEMPLATES = REPO_ROOT / "templates" / "vault"

TEMPLATE_FILES = [
    "CLAUDE.md",
    "Instructions/Instructions Home.md",
    "Instructions/Writing Style.md",
    "Instructions/Capture Protocol.md",
    "Instructions/Reminder and Calendar Routing.md",
    "11 - Templates/Daily Note.md",
    "Actions/Execution System.md",
    "Actions/Actions.base",
    "Brain Agents/Lessons/README.md",
    "00 - Inbox/README.md",
    "01 - Daily Notes/README.md",
]


def _template_paths() -> list[Path]:
    return sorted(
        p
        for p in TEMPLATES.rglob("*")
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(TEMPLATES).parts)
    )


def test_template_tree_is_exactly_the_listed_files():
    found = sorted(p.relative_to(TEMPLATES).as_posix() for p in _template_paths())
    assert found == sorted(TEMPLATE_FILES)


def test_init_creates_layout_in_empty_folder(tmp_path):
    target = tmp_path / "new vault"

    result = init_vault(target, DEFAULT_LAYOUT)

    assert sorted(result.created) == sorted(TEMPLATE_FILES)
    assert result.skipped == []
    for name in TEMPLATE_FILES:
        copied = target / name
        assert copied.is_file(), name
        assert copied.read_bytes() == (TEMPLATES / name).read_bytes()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o644
    assert (target / "CLAUDE.md").read_text(encoding="utf-8").strip()
    for folder in ["Instructions", "Actions", "Brain Agents", "Brain Agents/Lessons"]:
        assert stat.S_IMODE((target / folder).stat().st_mode) == 0o755
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_init_respects_layout_names(tmp_path):
    layout = VaultLayout(
        daily_notes_dir="Journal",
        inbox_dir="Inbox",
        templates_dir="Meta/Templates",
        actions_dir="Open Items",
        daily_note_template="Day.md",
    )

    result = init_vault(tmp_path, layout)

    assert (tmp_path / "Journal" / "README.md").is_file()
    assert not (tmp_path / "01 - Daily Notes").exists()
    assert (tmp_path / "Inbox" / "README.md").is_file()
    assert (tmp_path / "Meta" / "Templates" / "Day.md").read_bytes() == (
        TEMPLATES / "11 - Templates" / "Daily Note.md"
    ).read_bytes()
    assert (tmp_path / "Open Items" / "Actions.base").is_file()
    assert (tmp_path / "Open Items" / "Execution System.md").is_file()
    assert "Journal/README.md" in result.created
    assert "Meta/Templates/Day.md" in result.created
    assert not any(name.startswith(("00 - ", "01 - ", "11 - ", "Actions/")) for name in result.created)


def test_init_never_overwrites(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("keep", encoding="utf-8")

    first = init_vault(tmp_path, DEFAULT_LAYOUT)

    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "keep"
    assert first.skipped == ["CLAUDE.md"]
    assert "CLAUDE.md" not in first.created

    second = init_vault(tmp_path, DEFAULT_LAYOUT)

    assert second.created == []
    assert sorted(second.skipped) == sorted(TEMPLATE_FILES)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "keep"


def test_init_refuses_repo_path(tmp_path):
    with pytest.raises(ValueError):
        init_vault(REPO_ROOT, DEFAULT_LAYOUT)
    inside = REPO_ROOT / "vault-init-should-not-exist"
    with pytest.raises(ValueError):
        init_vault(inside, DEFAULT_LAYOUT)
    assert not inside.exists()


def test_templates_have_no_em_dash():
    with_dash = [
        path.relative_to(TEMPLATES).as_posix()
        for path in _template_paths()
        if "—" in path.read_text(encoding="utf-8")
    ]
    assert with_dash == []


def test_daily_note_template_has_the_two_placeholders():
    text = (TEMPLATES / "11 - Templates" / "Daily Note.md").read_text(encoding="utf-8")
    assert "{{date:YYYY-MM-DD}}" in text
    assert "{{date:dddd, YYYY-MM-DD}}" in text


def test_claude_md_template_has_sections_0_to_12():
    text = (TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(r"^## (\d+)\. ", text, flags=re.MULTILINE)]
    assert numbers == list(range(13))


def test_cli_init_vault_prints_created_and_skipped(tmp_path, monkeypatch, capsys):
    from src import cli

    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "profile.yaml").write_text(
        "notes:\n  daily_notes_dir: Journal\n", encoding="utf-8"
    )
    monkeypatch.setenv("USER_DIR", str(overlay))
    target = tmp_path / "vault"
    target.mkdir()
    (target / "CLAUDE.md").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["src.cli", "init-vault", str(target)])

    cli.main()

    created, skipped = capsys.readouterr().out.split("Skipped ")
    assert "  Journal/README.md" in created.splitlines()
    assert "  CLAUDE.md" not in created.splitlines()
    assert skipped.splitlines()[1:] == ["  CLAUDE.md"]
    assert (target / "Journal" / "README.md").is_file()
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == "keep"


PROBE_FILE = "vault-init-probe.md"


def _assert_refused_and_untouched(target: Path, probe: Path, tmp_path: Path) -> None:
    templates = tmp_path / "probe templates"
    templates.mkdir(exist_ok=True)
    (templates / PROBE_FILE).write_text("probe", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            init_vault(target, DEFAULT_LAYOUT, templates)
        assert not probe.exists()
    finally:
        if probe.is_dir():
            shutil.rmtree(probe)
        elif probe.exists():
            probe.unlink()


def test_init_refuses_a_case_changed_repo_path(tmp_path):
    changed = REPO_ROOT.parent / REPO_ROOT.name.swapcase()
    if changed == REPO_ROOT or not changed.exists():
        pytest.skip("this filesystem is case-sensitive")
    name = "vault-init-case-probe"
    _assert_refused_and_untouched(changed / name, REPO_ROOT / name, tmp_path)
    _assert_refused_and_untouched(changed, REPO_ROOT / PROBE_FILE, tmp_path)


def test_init_refuses_a_symlink_to_the_repo(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(REPO_ROOT, target_is_directory=True)
    name = "vault-init-link-probe"
    _assert_refused_and_untouched(link / name, REPO_ROOT / name, tmp_path)
    _assert_refused_and_untouched(link, REPO_ROOT / PROBE_FILE, tmp_path)


def test_init_refuses_the_repo_through_the_data_volume(tmp_path):
    other = Path("/System/Volumes/Data" + str(REPO_ROOT))
    if not other.exists():
        pytest.skip("no separate data volume path here")
    name = "vault-init-volume-probe"
    _assert_refused_and_untouched(other / name, REPO_ROOT / name, tmp_path)
    _assert_refused_and_untouched(other, REPO_ROOT / PROBE_FILE, tmp_path)


def test_template_sentences_match_the_code():
    claude = (TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8")
    lessons = (TEMPLATES / "Brain Agents" / "Lessons" / "README.md").read_text(encoding="utf-8")

    assert (
        "`Action Queue.md` in the daily notes folder belongs to the owner; "
        "agents never write it. Open items go to the open-items folder "
        "(`Actions/` by default)."
    ) in claude
    assert "Quick capture" not in claude and "Quick Capture" not in claude
    assert "append-only for agents" not in claude
    assert "Agents propose lessons; the owner adds them." in lessons
    assert "Agents append" not in lessons


def test_routing_note_says_where_the_table_lives():
    note = (TEMPLATES / "Instructions" / "Reminder and Calendar Routing.md").read_text(
        encoding="utf-8"
    )
    flat = " ".join(note.split())

    assert "`user/profile.yaml` under `reminders.routing`" in flat
    assert "in the framework folder, not in the vault" in flat
    assert "given to every chat through the connector's instructions" in flat


def test_cli_init_vault_under_a_file_prints_one_line(tmp_path, monkeypatch, capsys):
    from src import cli

    monkeypatch.setenv("USER_DIR", str(tmp_path / "overlay"))
    blocker = tmp_path / "a file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["src.cli", "init-vault", str(blocker / "vault")])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("init-vault: ")


def test_init_under_a_file_raises_oserror(tmp_path):
    blocker = tmp_path / "a file"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(OSError):
        init_vault(blocker / "vault", DEFAULT_LAYOUT)
