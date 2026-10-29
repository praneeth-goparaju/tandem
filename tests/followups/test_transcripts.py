import json
import os
from datetime import UTC, datetime
from pathlib import Path

from src.brain.followups.transcripts import iter_transcript_entries

FIX = Path(__file__).parent.parent / "fixtures" / "followups" / "projects"
SINCE = datetime(2026, 10, 10, tzinfo=UTC)
HOME = Path("/home/user")


def _read(
    projects_dir: Path,
    exclude: frozenset[str] = frozenset(),
    exclude_paths: frozenset[str] = frozenset(),
):
    return list(
        iter_transcript_entries(
            projects_dir,
            since=SINCE,
            home=HOME,
            exclude_projects=exclude,
            exclude_paths=exclude_paths,
        )
    )


def _user_line(
    content: str | list[dict],
    *,
    timestamp: str = "2026-10-14T06:00:00Z",
    cwd: str = "/home/user/w",
):
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": timestamp,
        "cwd": cwd,
        "isSidechain": False,
        "entrypoint": "cli",
    }


def _write_transcript(path: Path, lines: list[dict], *, modified: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    stamp = modified.timestamp()
    os.utime(path, (stamp, stamp))


def test_reader_keeps_typed_text_and_final_replies_only():
    entries = list(
        iter_transcript_entries(
            FIX, since=SINCE, home=Path("/home/user"), exclude_projects=frozenset()
        )
    )
    texts = [e.text for e in entries if e.project == "-Users-me-brain"]
    assert texts == [
        "Can you check the Garmin sync?",
        "I will look.",
        "also the weight chart please",
        "valid after broken",
    ]
    assert {e.who for e in entries} == {"owner", "claude"}
    assert all(e.source == "claude_code" and e.place == "private" for e in entries)
    assert entries[0].ref == "-Users-me-brain/s1.jsonl:1"


def test_an_excluded_project_also_excludes_its_worktrees_and_subfolders(tmp_path):
    folders = (
        "-Users-me-work-acme",
        "-Users-me-work-acme--worktrees-feature-x",
        "-Users-me-work-acme-api",
        "-Users-me-Work-Acme-web",
        "-Users-me-work-acmecorp",
        "-Users-me-brain",
    )
    for folder in folders:
        _write_transcript(
            tmp_path / folder / "s.jsonl",
            [_user_line(f"typed in {folder}")],
            modified=datetime(2026, 10, 14, tzinfo=UTC),
        )
    entries = _read(tmp_path, exclude=frozenset({"-Users-me-work-acme"}))
    assert sorted(e.project for e in entries) == ["-Users-me-brain", "-Users-me-work-acmecorp"]


def test_a_line_whose_working_directory_has_an_excluded_component_is_skipped(tmp_path):
    _write_transcript(
        tmp_path / "-Users-me" / "s.jsonl",
        [
            _user_line("in the client repo", cwd="/Users/me/work/acme/api"),
            _user_line("in the client repo, other case", cwd="/Users/me/Work/ACME"),
            _user_line("beside it", cwd="/Users/me/work/acme-notes"),
            _user_line("at home", cwd="/Users/me/brain"),
        ],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    entries = _read(tmp_path, exclude_paths=frozenset({"acme"}))
    assert [e.text for e in entries] == ["beside it", "at home"]


def test_the_reader_follows_no_symlinked_folder_or_file(tmp_path):
    outside = tmp_path / "outside"
    _write_transcript(
        outside / "elsewhere.jsonl",
        [_user_line("outside the projects folder")],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    projects = tmp_path / "projects"
    _write_transcript(
        projects / "-Users-me-brain" / "s.jsonl",
        [_user_line("a real transcript")],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    (projects / "-Users-me-linked").symlink_to(outside, target_is_directory=True)
    (projects / "-Users-me-brain" / "linked.jsonl").symlink_to(outside / "elsewhere.jsonl")
    assert [e.text for e in _read(projects)] == ["a real transcript"]


def test_reader_dates_each_entry_from_its_line_and_numbers_lines_from_one():
    by_text = {e.text: e for e in _read(FIX)}
    assert by_text["I will look."].who == "claude"
    assert by_text["I will look."].when == "2026-10-14T06:00:05Z"
    assert by_text["also the weight chart please"].ref == "-Users-me-brain/s1.jsonl:3"
    assert by_text["valid after broken"].ref == "-Users-me-brain/broken.jsonl:2"


def test_reader_drops_lines_dated_before_since_inside_a_recent_file(tmp_path):
    _write_transcript(
        tmp_path / "-Users-me-brain" / "s.jsonl",
        [
            _user_line("from last month", timestamp="2026-09-01T00:00:00Z"),
            _user_line("from this week"),
        ],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    entries = _read(tmp_path)
    assert [e.text for e in entries] == ["from this week"]
    assert entries[0].ref == "-Users-me-brain/s.jsonl:2"


def test_reader_does_not_read_files_untouched_since_the_cutoff(tmp_path):
    _write_transcript(
        tmp_path / "-Users-me-brain" / "stale.jsonl",
        [_user_line("never read")],
        modified=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert _read(tmp_path) == []


def test_reader_skips_only_working_directories_under_the_claude_folder(tmp_path):
    _write_transcript(
        tmp_path / "-Users-me-brain" / "s.jsonl",
        [
            _user_line("in the claude folder", cwd="/home/user/.claude"),
            _user_line("below the claude folder", cwd="/home/user/.claude/plugins/x"),
            _user_line("beside the claude folder", cwd="/home/user/.claude-notes"),
            _user_line("somewhere else", cwd="/home/user/work"),
        ],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert [e.text for e in _read(tmp_path)] == ["beside the claude folder", "somewhere else"]


def _assistant_line(text: str, *, timestamp: str = "2026-10-14T06:00:05Z"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": timestamp,
        "cwd": "/home/user/w",
        "isSidechain": False,
        "entrypoint": "cli",
    }


def test_reader_strips_system_reminder_blocks_from_typed_text_and_keeps_the_rest(tmp_path):
    reminder = "<system-reminder>\nThe task list is empty.\n</system-reminder>"
    as_blocks = [
        {"type": "text", "text": reminder},
        {"type": "text", "text": "move the dentist to Friday"},
    ]
    _write_transcript(
        tmp_path / "-Users-me-brain" / "s.jsonl",
        [_user_line(as_blocks), _user_line(f"call Sam back {reminder}")],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert [e.text for e in _read(tmp_path)] == ["move the dentist to Friday", "call Sam back"]


def test_reader_leaves_assistant_text_that_starts_with_a_tag_alone(tmp_path):
    _write_transcript(
        tmp_path / "-Users-me-brain" / "s.jsonl",
        [_assistant_line("<b>Done</b>, the chart is updated.")],
        modified=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert [e.text for e in _read(tmp_path)] == ["<b>Done</b>, the chart is updated."]
