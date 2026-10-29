"""Executable stdio MCP gateway."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from src.gateway import __version__
from src.gateway.backend import BackendError, BridgeSettings, CommandBackend
from src.gateway.profiles import ProfileConfigurationError, load_profile
from src.gateway.protocol import JsonRpcServer, serve_lines
from src.gateway.service import ToolService

SERVER_NAME = "brain-tools"


def _enabled(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def build_server(environment: Mapping[str, str]) -> JsonRpcServer:
    profile = load_profile(environment)
    settings = BridgeSettings.from_environment(environment)
    strict = _enabled(environment.get("BRAIN_MCP_STRICT_CATALOG"), default=True)
    service = ToolService(CommandBackend(settings), profile, strict_catalog=strict)
    return JsonRpcServer(
        service,
        server_name=SERVER_NAME,
        server_version=__version__,
        log=log,
    )


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        print(f"{SERVER_NAME} {__version__}")
        return
    if arguments:
        log("usage: brain-tools-mcp [--version]")
        raise SystemExit(2)

    try:
        server = build_server(os.environ)
        max_request = int(os.environ.get("BRAIN_MCP_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))
        if not 1024 <= max_request <= 16 * 1024 * 1024:
            raise ValueError("BRAIN_MCP_MAX_REQUEST_BYTES must be between 1 KiB and 16 MiB")
    except (BackendError, ProfileConfigurationError, ValueError) as exc:
        log(f"configuration error: {exc}")
        raise SystemExit(2) from exc

    serve_lines(
        sys.stdin.buffer,
        sys.stdout.buffer,
        server,
        max_request_bytes=max_request,
    )


if __name__ == "__main__":
    main()
