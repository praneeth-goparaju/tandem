from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_tool_registry_loads_without_housing_configuration(tmp_path: Path) -> None:
    user_dir = tmp_path / "empty-user"
    user_dir.mkdir()
    env = {**os.environ, "USER_DIR": str(user_dir)}

    result = subprocess.run(
        [sys.executable, "-m", "src.brain.mcp.pi_bridge", "list"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
