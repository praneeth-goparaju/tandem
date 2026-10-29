from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.brain.connector.vault.undo_store import UndoStore


def test_undo_store_file_mode_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "undo.db"
    old_umask = os.umask(0)
    try:
        store = UndoStore(db_path)
    finally:
        os.umask(old_umask)
    store.record("Notes/A.md", "old", "new", "a" * 64, 0)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_record_and_get_round_trip(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    before = datetime.now(UTC)
    edit_id = store.record("Notes/A.md", "old text", "new text", "b" * 64, 12)
    row = store.get(edit_id)
    assert (row.edit_id, row.rel, row.old, row.new, row.hash_after, row.start) == (
        edit_id,
        "Notes/A.md",
        "old text",
        "new text",
        "b" * 64,
        12,
    )
    assert row.created_at.tzinfo is not None
    assert before <= row.created_at <= datetime.now(UTC)


def test_ids_are_random_and_url_safe(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    first = store.record("Notes/A.md", "a", "b", "c" * 64, 0)
    second = store.record("Notes/A.md", "a", "b", "c" * 64, 0)
    held = store.hold("Notes/A.md", "a", "b", "c" * 64, "fix the typo here")
    assert len({first, second, held}) == 3
    for value in (first, second, held):
        assert len(value) >= 16
        assert all(ch.isalnum() or ch in "-_" for ch in value)


def test_missing_rows_raise_key_error(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    with pytest.raises(KeyError):
        store.get("nope")
    with pytest.raises(KeyError):
        store.take_held("nope")


def test_hold_and_take_held(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    held_id = store.hold("Notes/A.md", "old", "new", "d" * 64, "rename the section")
    row = store.take_held(held_id)
    assert (row.held_id, row.rel, row.old, row.new, row.expected_hash, row.reason) == (
        held_id,
        "Notes/A.md",
        "old",
        "new",
        "d" * 64,
        "rename the section",
    )
    store.drop_held(held_id)
    with pytest.raises(KeyError):
        store.take_held(held_id)


def test_edits_since_counts_recorded_edits(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    start = datetime.now(UTC)
    for _ in range(3):
        store.record("Notes/A.md", "a", "b", "c" * 64, 0)
    store.hold("Notes/A.md", "a", "b", "c" * 64, "held edits do not count")
    assert store.edits_since(start - timedelta(hours=1)) == 3
    assert store.edits_since(datetime.now(UTC) + timedelta(seconds=1)) == 0


def test_purge_after_90_days(tmp_path: Path) -> None:
    store = UndoStore(tmp_path / "undo.db")
    edit_id = store.record("Notes/A.md", "a", "b", "c" * 64, 0)
    held_id = store.hold("Notes/A.md", "a", "b", "c" * 64, "a held edit to keep")
    now = datetime.now(UTC)
    assert store.purge(now + timedelta(days=89)) == 0
    assert store.get(edit_id).edit_id == edit_id
    assert store.purge(now + timedelta(days=91)) == 2
    with pytest.raises(KeyError):
        store.get(edit_id)
    with pytest.raises(KeyError):
        store.take_held(held_id)
