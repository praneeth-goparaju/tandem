from __future__ import annotations

import logging
from pathlib import Path

import pytest


def test_private_log_handler_enforces_private_modes(tmp_path: Path) -> None:
    from src.orchestrator.scheduler import _private_log_handler

    directory = tmp_path / "logs"
    path = directory / "daemon.log"
    handler = _private_log_handler(path)
    try:
        handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None))
        handler.flush()
    finally:
        handler.close()

    assert directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8").strip() == "hello"


def test_private_log_handler_rejects_symlink_file(tmp_path: Path) -> None:
    from src.orchestrator.scheduler import _private_log_handler

    directory = tmp_path / "logs"
    directory.mkdir()
    directory.chmod(0o700)
    outside = tmp_path / "outside.log"
    outside.write_text("safe")
    (directory / "daemon.log").symlink_to(outside)

    with pytest.raises(RuntimeError, match="not a regular file"):
        _private_log_handler(directory / "daemon.log")
    assert outside.read_text() == "safe"


def test_private_log_handler_refuses_to_chmod_arbitrary_parent(tmp_path: Path) -> None:
    from src.orchestrator.scheduler import _private_log_handler

    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="must already be private"):
        _private_log_handler(directory / "daemon.log")
    assert directory.stat().st_mode & 0o777 == 0o755
    assert not (directory / "daemon.log").exists()
