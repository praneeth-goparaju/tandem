from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from src.brain.connector.layout import VaultLayout
from src.brain.connector.tools import Call, ToolEnv, run_tool, vault_tools
from src.brain.connector.vault.actions import open_item_add
from src.brain.connector.vault.undo_store import UndoStore

TODAY = date(2026, 10, 22)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    (root / "Actions").mkdir(parents=True)
    return root


def _frontmatter(text: str) -> dict[str, object]:
    _, block, _ = text.split("---\n", 2)
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict)
    return loaded


def test_first_item_of_day_is_01(vault: Path) -> None:
    result = open_item_add(vault, "Send the form", "next", today=TODAY)

    assert result.path == "Actions/A-20261022-01 - Send the form.md"
    assert (vault / result.path).read_text(encoding="utf-8") == result.text
    assert _frontmatter(result.text)["action_id"] == "A-20261022-01"


def test_first_item_creates_the_actions_folder(tmp_path: Path) -> None:
    result = open_item_add(tmp_path, "Send the form", "next", today=TODAY)

    assert (tmp_path / result.path).is_file()


def test_open_item_uses_layout_folder(tmp_path: Path) -> None:
    layout = VaultLayout(actions_dir="Open")
    (tmp_path / "Open").mkdir()
    (tmp_path / "Open" / "A-20261022-01 - One.md").write_text("x", encoding="utf-8")

    result = open_item_add(tmp_path, "Two", "next", today=TODAY, layout=layout)

    assert result.path == "Open/A-20261022-02 - Two.md"
    assert (tmp_path / result.path).is_file()
    assert not (tmp_path / "Actions").exists()


def test_next_free_number(vault: Path) -> None:
    (vault / "Actions" / "A-20261022-01 - One.md").write_text("x", encoding="utf-8")
    (vault / "Actions" / "A-20261022-03 - Three.md").write_text("x", encoding="utf-8")
    (vault / "Actions" / "A-20261021-09 - Other day.md").write_text("x", encoding="utf-8")

    result = open_item_add(vault, "Four", "next", today=TODAY)

    assert result.path == "Actions/A-20261022-04 - Four.md"


def test_waiting_requires_waiting_on_and_follow_up(vault: Path) -> None:
    with pytest.raises(ValueError):
        open_item_add(vault, "Hear back", "waiting", follow_up=date(2026, 12, 10), today=TODAY)
    with pytest.raises(ValueError):
        open_item_add(vault, "Hear back", "waiting", waiting_on="Amt", today=TODAY)
    assert not list((vault / "Actions").iterdir())


def test_empty_outcome_is_refused(vault: Path) -> None:
    with pytest.raises(ValueError):
        open_item_add(vault, "  \n ", "next", today=TODAY)
    with pytest.raises(ValueError):
        open_item_add(vault, "???", "next", today=TODAY)


def test_outcome_with_slash_is_sanitised(vault: Path) -> None:
    result = open_item_add(vault, 'Call A/B: "now"? <ok>|*', "next", today=TODAY)

    assert result.path == "Actions/A-20261022-01 - Call A-B now ok.md"
    assert '# Call A/B: "now"? <ok>|*\n' in result.text


def test_frontmatter_matches_trial_fields(vault: Path) -> None:
    result = open_item_add(
        vault,
        "Town hall answers the permit",
        "waiting",
        due=date(2026, 11, 17),
        waiting_on='Town hall "North"',
        follow_up=date(2026, 12, 10),
        source="Mail from the Straßenamt",
        today=TODAY,
    )

    assert _frontmatter(result.text) == {
        "type": "action",
        "action_id": "A-20261022-01",
        "status": "waiting",
        "priority": "normal",
        "due": date(2026, 11, 17),
        "waiting_on": 'Town hall "North"',
        "follow_up": date(2026, 12, 10),
        "added": TODAY,
        "updated": TODAY,
        "source": "Mail from the Straßenamt",
        "tags": ["action"],
    }
    assert list(_frontmatter(result.text)) == [
        "type", "action_id", "status", "priority", "due", "waiting_on",
        "follow_up", "added", "updated", "source", "tags",
    ]  # fmt: skip
    assert result.text.endswith(
        "---\n\n# Town hall answers the permit\n\nSource: Mail from the Straßenamt\n"
    )


def test_next_item_leaves_out_what_is_not_given(vault: Path) -> None:
    result = open_item_add(
        vault, "Book the dentist", "next", waiting_on="Nobody", follow_up=date(2026, 12, 1),
        today=TODAY,
    )  # fmt: skip

    assert set(_frontmatter(result.text)) == {
        "type",
        "action_id",
        "status",
        "priority",
        "added",
        "updated",
        "tags",
    }
    assert result.text.endswith("# Book the dentist\n")
    assert "Source:" not in result.text


@pytest.mark.parametrize("zone_name", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_default_day_is_today_in_the_process_zone(
    vault: Path, zone_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", zone_name)
    time.tzset()

    result = open_item_add(vault, "Whatever", "next")

    today = datetime.now(ZoneInfo(zone_name)).date()
    assert result.path.startswith(f"Actions/A-{today:%Y%m%d}-01 - ")
    assert _frontmatter(result.text)["added"] == _frontmatter(result.text)["updated"]


def test_long_outcome_still_creates_the_item(vault: Path) -> None:
    outcome = "Before signing any house contract Alex has checked " + "the land register " * 30

    result = open_item_add(vault, outcome, "next", today=date(2026, 11, 18))

    name = Path(result.path).name
    assert len(name.encode()) <= 160
    assert f"# {' '.join(outcome.split())}" in result.text
    assert (vault / result.path).is_file()


def test_outcome_ending_in_md_gives_one_extension(vault: Path) -> None:
    result = open_item_add(vault, "Check House Watch Log.md", "next", today=date(2026, 11, 18))

    assert result.path.endswith("Check House Watch Log.md")
    assert not result.path.endswith(".md.md")


def test_cut_never_splits_a_character(vault: Path) -> None:
    result = open_item_add(vault, "ä" * 300, "next", today=date(2026, 11, 18))

    Path(result.path).name.encode().decode()
    assert len(Path(result.path).name.encode()) <= 160


def test_an_item_names_its_owner_and_who_assigned_it(vault: Path) -> None:
    result = open_item_add(
        vault, "Book the dentist", "next", today=TODAY, owner="alex", assigned_by="sam"
    )

    front = _frontmatter(result.text)
    assert (front["owner"], front["assigned_by"]) == ("alex", "sam")
    keys = list(front)
    assert keys[keys.index("priority") + 1 : keys.index("priority") + 3] == [
        "owner",
        "assigned_by",
    ]


def _tool_run(vault: Path, tmp_path: Path, args: dict[str, Any], call: Call, people: tuple[str, ...]) -> str:
    env = ToolEnv(vault=vault, undo=UndoStore(tmp_path / "undo.db"))
    tool = next(tool for tool in vault_tools(people) if tool.name == "open_item_add")
    return asyncio.run(run_tool(tool, env, args, call))


SAM = Call(person="sam", person_name="Sam")


def test_sams_item_is_hers_unless_she_names_another_owner(vault: Path, tmp_path: Path) -> None:
    people = ("alex", "sam")
    own = _tool_run(vault, tmp_path, {"outcome": "Buy shoes", "status": "next"}, SAM, people)
    other = _tool_run(
        vault, tmp_path, {"outcome": "Fix bike", "status": "next", "owner": "alex"}, SAM, people
    )
    both = _tool_run(
        vault, tmp_path, {"outcome": "Plan trip", "status": "next", "owner": "both"}, SAM, people
    )
    stranger = _tool_run(
        vault, tmp_path, {"outcome": "Odd", "status": "next", "owner": "pat"}, SAM, people
    )

    assert "owner: sam\nassigned_by: sam\n" in own
    assert "owner: alex\nassigned_by: sam\n" in other
    assert "owner: both\nassigned_by: sam\n" in both
    assert stranger.startswith("ERROR: invalid arguments for open_item_add")
    assert len(list((vault / "Actions").iterdir())) == 3


def test_the_owner_argument_is_offered_only_when_there_are_people() -> None:
    def schema(people: tuple[str, ...]) -> dict[str, Any]:
        return next(t for t in vault_tools(people) if t.name == "open_item_add").schema

    shared = schema(("alex", "sam"))["properties"]["owner"]
    assert shared["enum"] == ["alex", "sam", "both"]
    assert "the person you are working with" in shared["description"]
    assert "owner" not in schema(())["properties"]
    assert "owner" not in schema(("alex",))["properties"]


def test_a_connector_without_people_writes_no_owner(vault: Path, tmp_path: Path) -> None:
    text = _tool_run(vault, tmp_path, {"outcome": "Buy shoes", "status": "next"}, Call(), ())

    assert "owner:" not in text and "assigned_by:" not in text


def test_one_named_person_owns_their_items(vault: Path, tmp_path: Path) -> None:
    alex = Call(person="alex", person_name="Alex")
    text = _tool_run(vault, tmp_path, {"outcome": "Buy shoes", "status": "next"}, alex, ("alex",))

    assert "owner: alex\nassigned_by: alex\n" in text
