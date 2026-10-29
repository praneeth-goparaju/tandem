from __future__ import annotations

import pytest

from src.gateway.profiles import ProfileConfigurationError
from src.gateway.server import build_server, main


def test_server_builds_in_read_mode_by_default(tmp_path) -> None:
    python = tmp_path / "python"
    python.write_text("placeholder", encoding="utf-8")

    server = build_server(
        {
            "BRAIN_MCP_BACKEND_DIR": str(tmp_path),
            "BRAIN_MCP_BACKEND_PYTHON": str(python),
        }
    )

    assert server.service.profile.name == "read"


def test_server_rejects_implicit_trusted_mode(tmp_path) -> None:
    with pytest.raises(ProfileConfigurationError, match="ALLOW_WRITES"):
        build_server(
            {
                "BRAIN_MCP_BACKEND_DIR": str(tmp_path),
                "BRAIN_MCP_PROFILE": "trusted",
            }
        )


def test_version_entrypoint(capsys) -> None:
    main(["--version"])

    assert capsys.readouterr().out.startswith("brain-tools 0.1.0")
