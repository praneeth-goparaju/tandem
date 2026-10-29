from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.brain.connector.audit import AuditLog


def _lines(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_one_line_per_call(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit")
    when = datetime(2026, 10, 22, 9, 30, 5, tzinfo=UTC)
    log.write("read_note", "Notes/A.md", "ok", "tok1", "path=Notes/A.md", now=when)
    log.write("edit_note", "Notes/B.md", "held", "tok2", "mode=replace", now=when)

    raw = (tmp_path / "audit" / "audit-2026-10.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    first, second = _lines(tmp_path / "audit" / "audit-2026-10.jsonl")
    assert first == {
        "time": "2026-10-22T09:30:05+00:00",
        "tool": "read_note",
        "target": "Notes/A.md",
        "result": "ok",
        "token_id": "tok1",
        "args": "path=Notes/A.md",
    }
    assert second["tool"] == "edit_note"
    assert second["result"] == "held"


def test_lines_have_sorted_keys_and_keep_unicode(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    when = datetime(2026, 10, 22, tzinfo=UTC)
    log.write("read_note", "Notes/Café.md", "ok", "t", "größe", now=when)
    raw = (tmp_path / "audit-2026-10.jsonl").read_text(encoding="utf-8")
    assert "Café" in raw
    assert "größe" in raw
    keys = list(json.loads(raw).keys())
    assert keys == sorted(keys)


def test_monthly_file_name(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.write("t", "x", "ok", "k", "", now=datetime(2026, 1, 31, 23, 59, tzinfo=UTC))
    log.write("t", "x", "ok", "k", "", now=datetime(2026, 2, 1, 0, 0, tzinfo=UTC))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "audit-2026-01.jsonl",
        "audit-2026-02.jsonl",
    ]


def test_time_is_utc_for_other_zones(tmp_path: Path) -> None:
    from datetime import timedelta, timezone

    berlin = timezone(timedelta(hours=2))
    log = AuditLog(tmp_path)
    log.write("t", "x", "ok", "k", "", now=datetime(2026, 3, 1, 1, 0, tzinfo=berlin))
    (entry,) = _lines(tmp_path / "audit-2026-02.jsonl")
    assert entry["time"] == "2026-02-28T23:00:00+00:00"


def test_default_now_is_current_utc_time(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    before = datetime.now(UTC)
    log.write("t", "x", "ok", "k", "")
    after = datetime.now(UTC)
    (path,) = tmp_path.iterdir()
    (entry,) = _lines(path)
    assert before <= datetime.fromisoformat(entry["time"]) <= after
    assert path.name == f"audit-{before:%Y-%m}.jsonl" or path.name == f"audit-{after:%Y-%m}.jsonl"


def test_long_text_truncated_to_200(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    when = datetime(2026, 10, 22, tzinfo=UTC)
    log.write("t", "x", "e" * 500, "k", "a" * 500, now=when)
    log.write("t", "x", "r" * 200, "k", "b" * 200, now=when)
    long_entry, exact_entry = _lines(tmp_path / "audit-2026-10.jsonl")
    assert long_entry["args"] == "a" * 200 + "..."
    assert long_entry["result"] == "e" * 200 + "..."
    assert exact_entry["args"] == "b" * 200
    assert exact_entry["result"] == "r" * 200


def test_text_with_newlines_stays_on_one_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.write("t", "x", "ok", "k", "line one\nline two", now=datetime(2026, 10, 1, tzinfo=UTC))
    path = tmp_path / "audit-2026-10.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert _lines(path)[0]["args"] == "line one\nline two"


def test_directory_and_file_modes(tmp_path: Path) -> None:
    old_umask = os.umask(0)
    try:
        log = AuditLog(tmp_path / "data" / "audit")
        log.write("t", "x", "ok", "k", "", now=datetime(2026, 10, 1, tzinfo=UTC))
    finally:
        os.umask(old_umask)
    directory = tmp_path / "data" / "audit"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "audit-2026-10.jsonl").stat().st_mode) == 0o600


def test_prune_keeps_12_months(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    for name in (
        "audit-2025-08.jsonl",
        "audit-2025-09.jsonl",
        "audit-2025-10.jsonl",
        "audit-2026-05.jsonl",
        "audit-2026-10.jsonl",
    ):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")

    removed = log.prune(datetime(2026, 10, 22, tzinfo=UTC))

    assert removed == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "audit-2025-10.jsonl",
        "audit-2026-05.jsonl",
        "audit-2026-10.jsonl",
    ]


def test_prune_across_a_year_boundary(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    for name in ("audit-2024-12.jsonl", "audit-2025-01.jsonl", "audit-2025-02.jsonl"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    assert log.prune(datetime(2026, 1, 15, tzinfo=UTC)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "audit-2025-01.jsonl",
        "audit-2025-02.jsonl",
    ]


def test_prune_ignores_other_files(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    others = [
        "notes.txt",
        "audit-2020-01.jsonl.bak",
        "audit-2020-13.jsonl",
        "audit-20-01.jsonl",
        "old-audit-2020-01.jsonl",
    ]
    for name in others:
        (tmp_path / name).write_text("keep", encoding="utf-8")
    (tmp_path / "audit-2020-01.jsonl").mkdir()

    assert log.prune(datetime(2026, 10, 22, tzinfo=UTC)) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*others, "audit-2020-01.jsonl"])


def test_prune_defaults_to_now_and_returns_zero_when_nothing_old(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.write("t", "x", "ok", "k", "")
    assert log.prune(datetime.now(UTC)) == 0
    assert len(list(tmp_path.iterdir())) == 1


def test_write_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = AuditLog(tmp_path / "audit")
    (tmp_path / "audit").chmod(0o500)
    try:
        if os.access(tmp_path / "audit", os.W_OK):
            pytest.skip("running with permissions that ignore directory modes")
        with caplog.at_level(logging.WARNING, logger="src.brain.connector.audit"):
            log.write("t", "x", "ok", "k", "", now=datetime(2026, 10, 1, tzinfo=UTC))
    finally:
        (tmp_path / "audit").chmod(0o700)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "audit" in caplog.records[0].getMessage()


def test_wrong_argument_type_still_raises(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    with pytest.raises(TypeError):
        log.write("t", "x", "ok", "k", 5, now=datetime(2026, 10, 1, tzinfo=UTC))  # type: ignore[arg-type]
