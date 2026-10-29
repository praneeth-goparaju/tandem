from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_housing_watch_changes_reject_scheduled_agent(monkeypatch) -> None:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools import housing
    from src.brain.mcp.tools.housing import unwatch_search, watch_search

    monkeypatch.setattr(housing, "require_configured", lambda: None)

    caller = ToolCaller(agent="housing", area="housing", is_reply=False)
    with caller_scope(caller):
        with pytest.raises(PermissionError, match="authenticated reply"):
            asyncio.run(watch_search({"name": "untrusted"}))
        with pytest.raises(PermissionError, match="authenticated reply"):
            asyncio.run(unwatch_search({"name": "untrusted"}))


def test_housing_database_and_parent_are_private(monkeypatch, tmp_path: Path) -> None:
    from src.brain.housing import storage

    parent = tmp_path / "housing-state"
    path = parent / "housing.db"
    monkeypatch.setattr(storage, "DB_PATH", path)

    conn = storage.get_db()
    try:
        assert conn.execute("SELECT count(*) FROM watches").fetchone()[0] == 0
        assert path.stat().st_mode & 0o777 == 0o600
        assert parent.stat().st_mode & 0o777 == 0o700
    finally:
        conn.close()


def test_housing_database_rejects_symlink_parent(monkeypatch, tmp_path: Path) -> None:
    from src.brain.housing import storage

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(storage, "DB_PATH", linked / "housing.db")

    with pytest.raises(OSError, match="path component"):
        storage.get_db()
    assert not (outside / "housing.db").exists()
