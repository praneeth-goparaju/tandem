from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.gateway.backend import (
    BackendError,
    BackendTimeoutError,
    BridgeSettings,
    CommandBackend,
)


def _settings(tmp_path: Path) -> BridgeSettings:
    return BridgeSettings(directory=tmp_path, python=Path(sys.executable), module="fake_bridge")


def _bridge(tmp_path: Path, body: str) -> None:
    (tmp_path / "fake_bridge.py").write_text(body, encoding="utf-8")


def test_backend_invokes_bridge_with_operation_and_payload(tmp_path) -> None:
    _bridge(
        tmp_path,
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'operation': sys.argv[1], 'payload': payload}))\n",
    )

    result = CommandBackend(_settings(tmp_path)).invoke("call", {"name": "x"})

    assert result == {"operation": "call", "payload": {"name": "x"}}


def test_backend_rejects_unknown_operation_before_launch(tmp_path) -> None:
    with pytest.raises(BackendError, match="unsupported"):
        CommandBackend(_settings(tmp_path)).invoke("delete-everything")


def test_backend_hides_invalid_json(tmp_path) -> None:
    _bridge(tmp_path, "print('not-json')\n")

    with pytest.raises(BackendError, match="invalid JSON"):
        CommandBackend(_settings(tmp_path)).invoke("list")


def test_backend_converts_timeout(tmp_path) -> None:
    _bridge(tmp_path, "import time\ntime.sleep(2)\n")
    settings = BridgeSettings(
        directory=tmp_path,
        python=Path(sys.executable),
        module="fake_bridge",
        timeout_seconds=0.1,
    )

    with pytest.raises(BackendTimeoutError, match="exceeded"):
        CommandBackend(settings).invoke("list")


def test_backend_kills_oversized_output(tmp_path) -> None:
    _bridge(tmp_path, "import sys\nsys.stdout.write('x' * 5000)\n")
    settings = BridgeSettings(
        directory=tmp_path,
        python=Path(sys.executable),
        module="fake_bridge",
        max_output_bytes=1024,
    )

    with pytest.raises(BackendError, match="exceeded"):
        CommandBackend(settings).invoke("list")


def test_backend_does_not_surface_private_stderr(tmp_path) -> None:
    _bridge(
        tmp_path,
        "import sys\nprint('/private/secret/path', file=sys.stderr)\nraise SystemExit(9)\n",
    )

    with pytest.raises(BackendError, match="backend list failed") as captured:
        CommandBackend(_settings(tmp_path)).invoke("list")

    assert "secret" not in str(captured.value)
