from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.brain.connector.config import Integrations
from src.brain.connector.instructions import (
    MAX_LINES,
    TEMPLATE_PATH,
    InstructionsOverlay,
    InstructionsTooLong,
    RoutingRow,
    SourceRow,
    load_overlay,
    render_instructions,
)
from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout

REPO_ROOT = Path(__file__).resolve().parents[2]
EM_DASH = chr(0x2014)
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "connector_instructions_default.md"
EXAMPLE = REPO_ROOT / "examples" / "connector_instructions.example.yaml"
ALL_OFF = Integrations()
REMINDERS_ON = Integrations(reminders=True)


def _full_overlay() -> InstructionsOverlay:
    return InstructionsOverlay(
        owner_name="Alex",
        privacy_rule="Surprises for Sam stay out of shared notes until Sam knows.",
        routing_rows=(
            RoutingRow(who="Child A", list="List A", calendar="Home"),
            RoutingRow(who="The household", list="List B", calendar="none"),
        ),
        source_of_truth=(
            SourceRow(topic="Topic One", file="Folder/One.md"),
            SourceRow(topic="Topic Two", file="Folder/Two.md"),
        ),
        extra_rules=("Ask before booking anything.",),
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "connector_instructions.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_render_full_overlay() -> None:
    text = render_instructions(_full_overlay(), DEFAULT_LAYOUT, REMINDERS_ON)
    lines = text.splitlines()

    assert "the owner" not in text
    assert "Alex" in text
    assert "| Child A | List A | Home |" in lines
    assert "| The household | List B | none |" in lines
    assert "| Topic One | `Folder/One.md` |" in lines
    assert "| Topic Two | `Folder/Two.md` |" in lines
    assert "7. When you create a reminder or an event, always set an alarm" in text
    assert "9. Surprises for Sam stay out of shared notes until Sam knows." in lines
    assert "10. Ask before booking anything." in lines


def test_rules_are_numbered_without_gaps() -> None:
    overlay = InstructionsOverlay(privacy_rule="Keep it private.", extra_rules=("One more.",))

    text = render_instructions(overlay, DEFAULT_LAYOUT, ALL_OFF)

    assert "7. Silence in the vault" in text
    assert "8. Keep it private." in text.splitlines()
    assert "9. One more." in text.splitlines()


def test_render_routing_only_with_integrations() -> None:
    overlay = _full_overlay()

    off = render_instructions(overlay, DEFAULT_LAYOUT, ALL_OFF)
    calendar = render_instructions(overlay, DEFAULT_LAYOUT, Integrations(calendar=True))
    mail_only = render_instructions(overlay, DEFAULT_LAYOUT, Integrations(mail=True))

    assert "Child A" not in off
    assert "alarm" not in off
    assert "Child A" in calendar
    assert "alarm" in calendar
    assert "Child A" not in mail_only


def test_no_routing_table_without_rows() -> None:
    text = render_instructions(InstructionsOverlay(), DEFAULT_LAYOUT, REMINDERS_ON)

    assert "alarm" in text
    assert "Reminders list" not in text


def test_daily_note_line_follows_the_layout() -> None:
    layout = VaultLayout(daily_notes_dir="Journal", actions_dir="Tasks/Open")

    text = render_instructions(InstructionsOverlay(), layout, ALL_OFF)

    assert "`Journal/YYYY/MM/YYYY-MM-DD.md`" in text
    assert "`Tasks/Open/`" in text
    assert "01 - Daily Notes" not in text


def test_too_long_refused() -> None:
    rows = tuple(SourceRow(topic=f"Topic {n}", file=f"Folder/{n}.md") for n in range(40))
    overlay = replace(_full_overlay(), source_of_truth=rows)

    with pytest.raises(InstructionsTooLong) as caught:
        render_instructions(overlay, DEFAULT_LAYOUT, REMINDERS_ON)

    assert caught.value.lines > MAX_LINES
    assert str(MAX_LINES) in str(caught.value)


def test_em_dash_refused() -> None:
    overlay = InstructionsOverlay(extra_rules=(f"Plain words {EM_DASH} always.",))

    with pytest.raises(ValueError, match="em dash"):
        render_instructions(overlay, DEFAULT_LAYOUT, ALL_OFF)


def test_default_render_matches_golden() -> None:
    text = render_instructions(InstructionsOverlay(), DEFAULT_LAYOUT, Integrations())

    assert text.encode("utf-8") == GOLDEN.read_bytes()


@pytest.mark.parametrize("path", [TEMPLATE_PATH, GOLDEN, EXAMPLE], ids=lambda p: p.name)
def test_shipped_instruction_files_have_no_em_dash(path: Path) -> None:
    assert EM_DASH not in path.read_text(encoding="utf-8")


def test_missing_overlay_gives_defaults(tmp_path: Path) -> None:
    assert load_overlay(tmp_path / "absent.yaml") == InstructionsOverlay()
    assert load_overlay(None) == InstructionsOverlay()


def test_overlay_is_read(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "owner_name: Alex\n"
        "privacy_rule: Keep it private.\n"
        "routing_rows:\n"
        "  - {who: Child A, list: List A, calendar: Home}\n"
        "  - {who: Groceries, list: List B}\n"
        "source_of_truth:\n"
        "  - {topic: Topic, file: Folder/Note.md}\n"
        "extra_rules:\n"
        "  - Ask before booking anything.\n",
    )

    assert load_overlay(path) == InstructionsOverlay(
        owner_name="Alex",
        privacy_rule="Keep it private.",
        routing_rows=(
            RoutingRow(who="Child A", list="List A", calendar="Home"),
            RoutingRow(who="Groceries", list="List B", calendar="none"),
        ),
        source_of_truth=(SourceRow(topic="Topic", file="Folder/Note.md"),),
        extra_rules=("Ask before booking anything.",),
    )


def test_shipped_example_overlay_loads_and_renders() -> None:
    overlay = load_overlay(EXAMPLE)

    assert overlay.routing_rows and overlay.source_of_truth and overlay.extra_rules
    text = render_instructions(overlay, DEFAULT_LAYOUT, Integrations(reminders=True))
    assert len(text.splitlines()) <= MAX_LINES


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("- a list\n", "mapping"),
        ("owner: Alex\n", "owner"),
        ("routing_rows:\n  - {list: List A}\n", "routing_rows[0].who"),
        ("routing_rows:\n  - {who: Child A}\n", "routing_rows[0].list"),
        ("routing_rows:\n  - {who: A, list: B, colour: red}\n", "routing_rows[0].colour"),
        ("source_of_truth:\n  - {file: Folder/Note.md}\n", "source_of_truth[0].topic"),
        ("source_of_truth:\n  - {topic: Topic}\n", "source_of_truth[0].file"),
        ("source_of_truth: Topic\n", "source_of_truth"),
        ("extra_rules:\n  - 3\n", "extra_rules[0]"),
        ("owner_name: ''\n", "owner_name"),
        ('routing_rows:\n  - {who: "A\\nB", list: L}\n', "routing_rows[0].who"),
        ('extra_rules:\n  - "two\\nlines"\n', "extra_rules[0]"),
        (f"privacy_rule: Quiet {EM_DASH} please\n", "privacy_rule"),
        ('privacy_rule: "one\\u2028two"\n', "privacy_rule"),
        ('routing_rows:\n  - {who: "A\\u2029B", list: L}\n', "routing_rows[0].who"),
        ('extra_rules:\n  - "a\\x0bb"\n', "extra_rules[0]"),
        ('extra_rules:\n  - "a\\x85b"\n', "extra_rules[0]"),
        ("extra_rules:\n  - '   '\n", "extra_rules[0]"),
    ],
)
def test_bad_overlay_refused(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ValueError, match=key.replace("[", r"\[").replace("]", r"\]")):
        load_overlay(_write(tmp_path, text))


def _profile(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


PROFILE_ROUTING = (
    "notes:\n"
    "  daily_notes_dir: Journal\n"
    "reminders:\n"
    "  default_list: List B\n"
    "  routing:\n"
    "    - {who: Child A, list: List A, calendar: Home}\n"
    "    - {who: Groceries, list: List B}\n"
)


def test_routing_rows_come_from_the_profile(tmp_path: Path) -> None:
    profile = _profile(tmp_path, PROFILE_ROUTING)
    overlay_path = _write(tmp_path, "")

    overlay = load_overlay(overlay_path, profile)
    text = render_instructions(overlay, DEFAULT_LAYOUT, REMINDERS_ON)

    assert overlay.routing_rows == (
        RoutingRow(who="Child A", list="List A", calendar="Home"),
        RoutingRow(who="Groceries", list="List B", calendar="none"),
    )
    assert "| Child A | List A | Home |" in text.splitlines()
    assert "| Groceries | List B | none |" in text.splitlines()
    assert load_overlay(None, profile).routing_rows == overlay.routing_rows


def test_overlay_routing_rows_win_over_the_profile(tmp_path: Path) -> None:
    profile = _profile(tmp_path, PROFILE_ROUTING)
    overlay_path = _write(tmp_path, "routing_rows:\n  - {who: The household, list: List C}\n")

    overlay = load_overlay(overlay_path, profile)

    assert overlay.routing_rows == (RoutingRow(who="The household", list="List C"),)


def test_profile_without_routing_gives_no_rows(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "notes:\n  inbox_dir: Inbox\n")

    assert load_overlay(None, profile).routing_rows == ()
    assert load_overlay(None, tmp_path / "absent.yaml").routing_rows == ()


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("reminders:\n  routing:\n    - {who: Child A}\n", "reminders.routing[0].list"),
        ("reminders:\n  routing:\n    - {who: A, list: B, colour: red}\n", "reminders.routing[0].colour"),
        ("reminders:\n  routing:\n    - {who: 'A | B', list: L}\n", "reminders.routing[0].who"),
        ("reminders:\n  routing: Child A\n", "reminders.routing"),
        ("reminders: [a]\n", "reminders"),
    ],
)
def test_bad_profile_routing_refused(tmp_path: Path, text: str, key: str) -> None:
    profile = _profile(tmp_path, text)
    with pytest.raises(ValueError, match=key.replace("[", r"\[").replace("]", r"\]")):
        load_overlay(None, profile)
