from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def private_overlay_is_a_temp_folder(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``USER_DIR`` at an empty temp folder so no connector test reads ``user/``."""
    overlay = tmp_path_factory.mktemp("overlay")
    monkeypatch.setenv("USER_DIR", str(overlay))
    return overlay
