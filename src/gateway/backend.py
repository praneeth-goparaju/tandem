"""Subprocess adapter for the private Brain tool backend."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_MODULE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


class BackendError(RuntimeError):
    """The backend could not safely complete a bridge operation."""


class BackendTimeoutError(BackendError):
    """The backend exceeded its configured deadline."""


@dataclass(frozen=True)
class BridgeSettings:
    directory: Path
    python: Path
    module: str = "src.brain.mcp.pi_bridge"
    timeout_seconds: float = 60.0
    max_output_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> BridgeSettings:
        raw_directory = environment.get(
            "BRAIN_MCP_BACKEND_DIR",
            environment.get("BRAIN_DIR", str(Path.home() / "brain")),
        )
        directory = Path(raw_directory).expanduser()
        raw_python = environment.get(
            "BRAIN_MCP_BACKEND_PYTHON",
            environment.get("BRAIN_PYTHON", str(directory / ".venv" / "bin" / "python")),
        )
        module = environment.get("BRAIN_MCP_BACKEND_MODULE", cls.module).strip()
        if not _MODULE_PATTERN.fullmatch(module):
            raise BackendError("BRAIN_MCP_BACKEND_MODULE must be a dotted Python name")

        try:
            timeout = float(environment.get("BRAIN_MCP_TIMEOUT_SECONDS", "60"))
            max_output = int(environment.get("BRAIN_MCP_MAX_OUTPUT_BYTES", str(16 * 1024 * 1024)))
        except ValueError as exc:
            raise BackendError("backend timeout and output limit must be numeric") from exc
        if not 0.1 <= timeout <= 300:
            raise BackendError("BRAIN_MCP_TIMEOUT_SECONDS must be between 0.1 and 300")
        if not 1024 <= max_output <= 64 * 1024 * 1024:
            raise BackendError("BRAIN_MCP_MAX_OUTPUT_BYTES must be between 1 KiB and 64 MiB")

        return cls(
            directory=directory,
            python=Path(raw_python).expanduser(),
            module=module,
            timeout_seconds=timeout,
            max_output_bytes=max_output,
        )


class CommandBackend:
    """Invoke a one-shot JSON bridge without a shell."""

    def __init__(self, settings: BridgeSettings):
        self.settings = settings

    def invoke(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        if operation not in {"list", "call"}:
            raise BackendError(f"unsupported backend operation: {operation}")
        if not self.settings.directory.is_dir():
            raise BackendError("configured Brain backend directory does not exist")
        if not self.settings.python.is_file():
            raise BackendError("configured Brain Python interpreter does not exist")

        request = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        command = [
            str(self.settings.python),
            "-m",
            self.settings.module,
            operation,
        ]
        completed = _run_bounded(
            command,
            cwd=self.settings.directory,
            request=request,
            timeout_seconds=self.settings.timeout_seconds,
            max_stdout_bytes=self.settings.max_output_bytes,
            max_stderr_bytes=min(self.settings.max_output_bytes, 64 * 1024),
        )
        if completed.returncode != 0:
            raise BackendError(f"backend {operation} failed")
        try:
            return json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(f"backend {operation} returned invalid JSON") from exc


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: bytes


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    request: bytes,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> _Completed:
    """Run a bridge while bounding both captured streams during execution."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed executable and no shell
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
    except OSError as exc:
        raise BackendError(f"could not start backend: {exc.strerror}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout, max_stdout_bytes, overflow, process),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr, max_stderr_bytes, overflow, process),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    try:
        try:
            process.stdin.write(request)
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise BackendTimeoutError(f"backend exceeded {timeout_seconds:g} seconds") from exc
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        for reader in readers:
            reader.join(timeout=1)

    if overflow.is_set():
        raise BackendError("backend output exceeded the configured limit")
    return _Completed(returncode=returncode, stdout=bytes(stdout))


def _drain_bounded(
    stream: BinaryIO,
    target: bytearray,
    limit: int,
    overflow: threading.Event,
    process: subprocess.Popen[bytes],
) -> None:
    while chunk := stream.read(64 * 1024):
        remaining = limit - len(target)
        if remaining > 0:
            target.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflow.set()
            process.kill()
            return
