"""The daily change log: the audit log and the nightly backup, written as one note per day."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.brain.connector import change_log
from src.brain.connector.change_log import (
    WRITE_TOOLS,
    Change,
    main,
    outside_friday,
    render,
    through_friday,
)
from src.brain.connector.config import ConnectorConfig
from src.brain.connector.people import Person

ZONE = ZoneInfo("Europe/Berlin")
DAY = date(2026, 11, 18)
NAMES = {"alex": "Alex", "sam": "Sam"}


def _line(
    time: str,
    tool: str,
    target: str,
    *,
    person: str | None = "alex",
    result: str = "ok",
    result_id: str = "",
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "time": time,
        "tool": tool,
        "target": target,
        "result": result,
        "token_id": "tok",
        "args": {},
    }
    if person is not None:
        line.update(
            {"person": person, "sign_in_id": "abc", "endpoint": "full", "result_id": result_id}
        )
    return line


def _write_audit(folder: Path, month: str, lines: list[Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    with (folder / f"audit-{month}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


# Local times in these tests are Berlin, UTC+1 in November.


def test_two_people_give_two_rows_with_local_times(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-18T08:15:00+00:00", "vault_append", "Notes/A.md", person="alex"),
            _line("2026-11-18T13:40:30+00:00", "vault_create", "Notes/B.md", person="sam"),
            _line("2026-11-18T09:00:00+00:00", "vault_read", "Notes/C.md", person="alex"),
        ],
    )
    changes = through_friday(audit_dir, DAY, ZONE)
    assert changes == [
        Change("09:15", "alex", "vault_append", "Notes/A.md"),
        Change("14:40", "sam", "vault_create", "Notes/B.md"),
    ]
    text = render(DAY, changes, [], NAMES)
    assert "| 09:15 | Alex | added to | [[Notes/A]] |" in text
    assert "| 14:40 | Sam | created | [[Notes/B]] |" in text


def test_a_day_spans_two_monthly_files(audit_dir: Path) -> None:
    # 1 December in Berlin starts at 23:00 UTC on 30 November.
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-30T22:59:00+00:00", "vault_create", "Notes/Before.md"),
            _line("2026-11-30T23:30:00+00:00", "vault_create", "Notes/Late.md"),
        ],
    )
    _write_audit(
        audit_dir,
        "2026-12",
        [
            _line("2026-12-01T10:00:00+00:00", "vault_create", "Notes/Morning.md"),
            _line("2026-12-01T22:59:59+00:00", "vault_create", "Notes/Last.md"),
            _line("2026-12-01T23:00:00+00:00", "vault_create", "Notes/Next.md"),
        ],
    )
    notes = [c.note for c in through_friday(audit_dir, date(2026, 12, 1), ZONE)]
    assert notes == ["Notes/Late.md", "Notes/Morning.md", "Notes/Last.md"]
    first = through_friday(audit_dir, date(2026, 12, 1), ZONE)[0]
    assert first.time == "00:30"


def test_error_and_held_lines_are_left_out(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-18T08:00:00+00:00", "vault_edit", "Notes/A.md", result="error: stale"),
            _line("2026-11-18T08:01:00+00:00", "vault_edit", "Notes/A.md", result="held: rule"),
            _line("2026-11-18T08:02:00+00:00", "vault_append", "Notes/B.md"),
        ],
    )
    assert [c.note for c in through_friday(audit_dir, DAY, ZONE)] == ["Notes/B.md"]


def test_confirmed_held_edit_shows_the_note_once_under_the_confirming_person(
    audit_dir: Path,
) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line(
                "2026-11-18T08:00:00+00:00",
                "vault_edit",
                "Notes/Held.md",
                person="sam",
                result="held: rule",
                result_id="held-1",
            ),
            _line(
                "2026-11-18T08:05:00+00:00",
                "vault_edit_confirm",
                "held-1",
                person="alex",
                result_id="edit-1",
            ),
        ],
    )
    changes = through_friday(audit_dir, DAY, ZONE)
    assert changes == [Change("09:05", "alex", "vault_edit_confirm", "Notes/Held.md")]
    text = render(DAY, changes, [], NAMES)
    assert text.count("[[Notes/Held]]") == 1
    assert "| 09:05 | Alex | edited after approval | [[Notes/Held]] |" in text


def test_undo_finds_its_note_in_an_earlier_day(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-10",
        [_line("2026-10-20T08:00:00+00:00", "vault_edit", "Notes/Old.md", result_id="edit-9")],
    )
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-18T08:00:00+00:00", "vault_undo", "edit-9"),
            _line("2026-11-18T08:01:00+00:00", "vault_undo", "edit-unknown"),
        ],
    )
    changes = through_friday(audit_dir, DAY, ZONE)
    assert [c.note for c in changes] == ["Notes/Old.md", "(note not in the log)"]
    assert "undid an edit in | [[Notes/Old]]" in render(DAY, changes, [], NAMES)


def test_undo_of_a_confirmed_edit_finds_the_note_through_the_held_id(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-17T08:00:00+00:00", "vault_edit", "Notes/H.md", result="held: r",
                  result_id="held-2"),
            _line("2026-11-17T08:01:00+00:00", "vault_edit_confirm", "held-2", result_id="edit-2"),
            _line("2026-11-18T08:00:00+00:00", "vault_undo", "edit-2"),
        ],
    )
    assert [c.note for c in through_friday(audit_dir, DAY, ZONE)] == ["Notes/H.md"]


def test_a_matching_line_must_be_earlier(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [
            _line("2026-11-18T08:00:00+00:00", "vault_undo", "edit-5"),
            _line("2026-11-18T09:00:00+00:00", "vault_edit", "Notes/Later.md", result_id="edit-5"),
        ],
    )
    notes = [c.note for c in through_friday(audit_dir, DAY, ZONE)]
    assert notes == ["(note not in the log)", "Notes/Later.md"]


def test_open_item_shows_the_outcome(audit_dir: Path) -> None:
    _write_audit(
        audit_dir,
        "2026-11",
        [_line("2026-11-18T08:00:00+00:00", "open_item_add", "Call the plumber | Friday")],
    )
    changes = through_friday(audit_dir, DAY, ZONE)
    assert changes[0].note == "Actions: Call the plumber | Friday"
    text = render(DAY, changes, [], NAMES)
    assert "| open item | Actions: Call the plumber \\| Friday |" in text


def test_old_lines_without_a_person_belong_to_the_empty_prefix_person(audit_dir: Path) -> None:
    old = _line("2026-11-18T08:00:00+00:00", "vault_append", "Notes/A.md", person=None)
    old["args"] = '{"path": "Notes/A.md", "text": "<123 characters>"}'
    _write_audit(audit_dir, "2026-11", [old])
    alex = Person(
        key="alex", name="Alex", prefix="", excluded_tools=frozenset(), icloud_apple_id=None,
        icloud_imap_user=None, reminder_lists=None, calendars=None,
        layout=_layout(), instructions_overlay=Path("x"),
    )
    sam = Person(
        key="sam", name="Sam", prefix="sam", excluded_tools=frozenset(), icloud_apple_id=None,
        icloud_imap_user=None, reminder_lists=None, calendars=None,
        layout=_layout("01 - Daily Notes/Sam"), instructions_overlay=Path("y"),
    )
    changes = through_friday(audit_dir, DAY, ZONE, people=(sam, alex))
    assert changes == [Change("09:00", "alex", "vault_append", "Notes/A.md")]
    assert "| 09:00 | Alex | added to |" in render(DAY, changes, [], NAMES)


def _layout(daily: str = "01 - Daily Notes") -> Any:
    from dataclasses import replace

    from src.brain.connector.layout import DEFAULT_LAYOUT

    return replace(DEFAULT_LAYOUT, daily_notes_dir=daily)


def test_damaged_lines_never_stop_the_reader(audit_dir: Path) -> None:
    truncated = (
        '{"args": "{\\"path\\": \\"Notes/Cut.md\\", \\"text\\": \\"abc',
    )[0]
    _write_audit(
        audit_dir,
        "2026-11",
        [
            "not json at all",
            truncated,
            "",
            "[1, 2, 3]",
            '{"time": "garbage", "tool": "vault_append", "result": "ok"}',
            '{"tool": "vault_append", "result": "ok"}',
            _line("2026-11-18T08:00:00+00:00", "vault_create", "Notes/Fine.md"),
        ],
    )
    assert [c.note for c in through_friday(audit_dir, DAY, ZONE)] == ["Notes/Fine.md"]


def test_a_missing_audit_folder_gives_no_changes(tmp_path: Path) -> None:
    assert through_friday(tmp_path / "nowhere", DAY, ZONE) == []


def test_append_without_a_path_is_that_persons_daily_note(audit_dir: Path) -> None:
    alex = _person("alex", "", _layout())
    sam = _person("sam", "sam", _layout("01 - Daily Notes/Sam"))
    _write_audit(
        audit_dir,
        "2026-11",
        [
            # 23:30 UTC on the 17th is already the 18th in Berlin.
            _line("2026-11-17T23:30:00+00:00", "vault_append", "", person="sam"),
            _line("2026-11-18T10:00:00+00:00", "vault_append", "", person="alex"),
        ],
    )
    changes = through_friday(audit_dir, DAY, ZONE, people=(alex, sam))
    assert [c.note for c in changes] == [
        "01 - Daily Notes/Sam/2026/11/2026-11-18.md",
        "01 - Daily Notes/2026/11/2026-11-18.md",
    ]


def _person(key: str, prefix: str, layout: Any) -> Person:
    return Person(
        key=key, name=key.title(), prefix=prefix, excluded_tools=frozenset(),
        icloud_apple_id=None, icloud_imap_user=None, reminder_lists=None, calendars=None,
        layout=layout, instructions_overlay=Path(key),
    )


def test_write_tools_are_the_six_that_change_the_vault() -> None:
    assert WRITE_TOOLS == frozenset(
        {"vault_append", "vault_create", "vault_edit", "vault_edit_confirm", "vault_undo",
         "open_item_add"}
    )


# ---- the nightly backup ------------------------------------------------------------------


class Backup:
    """A bare Git repository whose work tree is a temporary vault, as the nightly backup has it."""

    def __init__(self, root: Path) -> None:
        self.repo = root / "backup.git"
        self.vault = root / "work"
        self.vault.mkdir()
        self._git("init", "--bare", "-q", str(self.repo), use_dir=False)

    def _git(self, *args: str, use_dir: bool = True, date: str = "") -> None:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.vault.parent)}
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        prefix = ["git", f"--git-dir={self.repo}", f"--work-tree={self.vault}"] if use_dir else ["git"]
        subprocess.run([*prefix, *args], cwd=self.vault, env=env, check=True, capture_output=True)

    def write(self, rel: str, text: str) -> None:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def remove(self, rel: str) -> None:
        (self.vault / rel).unlink()

    def commit(self, when: str) -> None:
        self._git("add", "-A")
        self._git(
            "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m",
            f"auto: nightly backup {when}", date=when,
        )


@pytest.fixture
def backup(tmp_path: Path) -> Backup:
    return Backup(tmp_path)


def _two_nights(backup: Backup) -> None:
    # The night before 18 November (Berlin) and the night of the 18th, both 23:50 local.
    backup.write("Notes/Through.md", "one")
    backup.write("Notes/Outside.md", "one")
    backup.write("Notes/Same.md", "one")
    backup.commit("2026-11-17T23:50:00+01:00")
    backup.write("Notes/Through.md", "two")
    backup.write("Notes/Outside.md", "two")
    backup.write("Notes/Brand New.md", "new")
    backup.write("Change Log/2026-11-17.md", "log")
    backup.write(".obsidian/workspace.json", "{}")
    backup.write("Notes/.hidden/secret.md", "x")
    backup.remove("Notes/Same.md")
    backup.commit("2026-11-18T23:50:00+01:00")


def test_outside_friday_lists_what_changed_and_was_not_through_friday(backup: Backup) -> None:
    _two_nights(backup)
    outside = outside_friday(backup.repo, DAY, {"Notes/Through.md"}, zone=ZONE)
    assert outside == ["Notes/Brand New.md", "Notes/Outside.md", "Notes/Same.md"]


def test_outside_friday_ignores_a_commit_after_the_day(backup: Backup) -> None:
    _two_nights(backup)
    backup.write("Notes/Next Day.md", "n")
    backup.commit("2026-11-19T23:50:00+01:00")
    assert "Notes/Next Day.md" not in outside_friday(backup.repo, DAY, set(), zone=ZONE)


def test_outside_friday_is_empty_without_a_commit_on_the_day(backup: Backup) -> None:
    backup.write("Notes/A.md", "one")
    backup.commit("2026-11-17T23:50:00+01:00")
    backup.write("Notes/A.md", "two")
    backup.commit("2026-11-19T23:50:00+01:00")
    assert outside_friday(backup.repo, DAY, set(), zone=ZONE) == []


def test_outside_friday_is_empty_without_an_earlier_commit(backup: Backup) -> None:
    backup.write("Notes/A.md", "one")
    backup.commit("2026-11-18T23:50:00+01:00")
    assert outside_friday(backup.repo, DAY, set(), zone=ZONE) == []


def test_outside_friday_is_empty_for_a_missing_repository(tmp_path: Path) -> None:
    assert outside_friday(tmp_path / "no-such.git", DAY, set(), zone=ZONE) == []


def test_outside_friday_is_empty_for_an_empty_repository(backup: Backup) -> None:
    assert outside_friday(backup.repo, DAY, set(), zone=ZONE) == []


def test_outside_friday_is_empty_when_the_folder_is_not_a_repository(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert outside_friday(tmp_path / "plain", DAY, set(), zone=ZONE) == []


def test_outside_friday_is_empty_when_git_is_not_found(
    backup: Backup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_nights(backup)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert outside_friday(backup.repo, DAY, set(), zone=ZONE) == []


def test_open_items_made_through_friday_are_not_listed_as_outside(backup: Backup) -> None:
    backup.write("Notes/A.md", "one")
    backup.commit("2026-11-17T23:50:00+01:00")
    backup.write("Actions/Call the plumber.md", "x")
    backup.write("Actions/Typed by hand.md", "y")
    backup.commit("2026-11-18T23:50:00+01:00")
    outside = outside_friday(
        backup.repo, DAY, set(), zone=ZONE, open_items_dir="Actions", open_items_made=1
    )
    assert len(outside) == 1


# ---- render ------------------------------------------------------------------------------


def test_render_a_full_note() -> None:
    changes = [
        Change("09:15", "alex", "vault_append", "Notes/A.md"),
        Change("10:00", "sam", "vault_edit", "Notes/B.md"),
        Change("11:00", "sam", "open_item_add", "Actions: Buy milk"),
        Change("12:00", "ghost", "vault_undo", "(note not in the log)"),
    ]
    text = render(DAY, changes, ["Notes/Outside.md", "Attachments/x.png"], NAMES)
    print(text)
    assert text == (
        "---\n"
        "type: change-log\n"
        "date: 2026-11-18\n"
        "---\n"
        "\n"
        "# Change log 2026-11-18\n"
        "\n"
        "## Through Friday\n"
        "\n"
        "| Time | Person | Change | Note |\n"
        "| --- | --- | --- | --- |\n"
        "| 09:15 | Alex | added to | [[Notes/A]] |\n"
        "| 10:00 | Sam | edited | [[Notes/B]] |\n"
        "| 11:00 | Sam | open item | Actions: Buy milk |\n"
        "| 12:00 | ghost | undid an edit in | (note not in the log) |\n"
        "\n"
        "## Changed outside Friday\n"
        "\n"
        "- [[Notes/Outside]]\n"
        "- [[Attachments/x.png]]\n"
    )
    assert "\u2014" not in text


def test_render_with_nothing_says_so_in_a_sentence() -> None:
    text = render(DAY, [], [], NAMES)
    assert "\u2014" not in text
    assert "Nothing was changed through Friday on this day." in text
    assert "No other changed notes were found in the nightly backup." in text
    assert "| Time |" not in text


def test_a_pipe_in_a_note_name_is_escaped_in_the_cell() -> None:
    text = render(DAY, [Change("09:00", "alex", "vault_create", "Notes/a|b.md")], [], NAMES)
    assert "| [[Notes/a\\|b]] |" in text


# ---- main --------------------------------------------------------------------------------


def _config(tmp_path: Path, people: tuple[Person, ...] = ()) -> ConnectorConfig:
    vault = tmp_path / "Vault"
    vault.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return ConnectorConfig(
        vault_path=vault,
        data_dir=data,
        public_url="https://vault.example.com",
        allowed_redirect_uris=(),
        timezone="Europe/Berlin",
        people=people,
    )


@pytest.fixture
def configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., ConnectorConfig]:
    def build(people: tuple[Person, ...] = ()) -> ConnectorConfig:
        config = _config(tmp_path, people)
        monkeypatch.setattr(change_log, "load_config", lambda *a, **k: config)
        return config

    return build


def test_main_writes_the_note_and_a_second_run_leaves_it_alone(
    tmp_path: Path, configured: Callable[..., ConnectorConfig], backup: Backup
) -> None:
    alex = _person("alex", "", _layout())
    sam = _person("sam", "sam", _layout("01 - Daily Notes/Sam"))
    config = configured((alex, sam))
    _write_audit(
        config.data_dir / "audit",
        "2026-11",
        [
            _line("2026-11-18T08:15:00+00:00", "vault_append", "Notes/Through.md", person="alex"),
            _line("2026-11-18T09:00:00+00:00", "vault_create", "Notes/New.md", person="sam"),
            _line("2026-11-18T10:00:00+00:00", "vault_append", "", person="sam"),
        ],
    )
    _two_nights(backup)

    code = main(["--day", "2026-11-18", "--backup-repo", str(backup.repo)])
    assert code == 0
    note = config.vault_path / "Change Log" / "2026-11-18.md"
    text = note.read_text(encoding="utf-8")
    print(text)
    assert text.startswith("---\ntype: change-log\ndate: 2026-11-18\n---\n")
    assert "| 09:15 | Alex | added to | [[Notes/Through]] |" in text
    assert "| 10:00 | Sam | created | [[Notes/New]] |" in text
    assert "[[01 - Daily Notes/Sam/2026/11/2026-11-18]]" in text
    changed_outside = text.split("## Changed outside Friday")[1]
    assert "[[Notes/Outside]]" in changed_outside
    assert "[[Notes/Through]]" not in changed_outside

    note.write_text("edited by hand\n", encoding="utf-8")
    assert main(["--day", "2026-11-18", "--backup-repo", str(backup.repo)]) == 0
    assert note.read_text(encoding="utf-8") == "edited by hand\n"


def test_main_with_a_missing_backup_repository_still_writes_the_note(
    tmp_path: Path, configured: Callable[..., ConnectorConfig]
) -> None:
    config = configured()
    _write_audit(
        config.data_dir / "audit",
        "2026-11",
        [_line("2026-11-18T08:15:00+00:00", "vault_create", "Notes/A.md", person="owner")],
    )
    code = main(["--day", "2026-11-18", "--backup-repo", str(tmp_path / "missing.git")])
    assert code == 0
    text = (config.vault_path / "Change Log" / "2026-11-18.md").read_text(encoding="utf-8")
    assert "[[Notes/A]]" in text
    assert "No other changed notes were found in the nightly backup." in text


def test_main_defaults_to_yesterday(
    tmp_path: Path, configured: Callable[..., ConnectorConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured()
    monkeypatch.setattr(change_log, "_today", lambda zone: date(2026, 11, 19))
    assert main(["--backup-repo", str(tmp_path / "missing.git")]) == 0
    assert (config.vault_path / "Change Log" / "2026-11-18.md").is_file()


def test_main_rejects_a_bad_day(configured: Callable[..., ConnectorConfig]) -> None:
    configured()
    with pytest.raises(SystemExit):
        main(["--day", "18.11.2026"])


def test_main_reports_a_missing_settings_file_without_a_trace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        raise FileNotFoundError("settings")

    monkeypatch.setattr(change_log, "load_config", refuse)
    assert main(["--day", "2026-11-18"]) == 1
    assert "settings" in capsys.readouterr().err
