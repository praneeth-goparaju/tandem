from __future__ import annotations

from pathlib import Path

import pytest

from src.brain.connector.__main__ import main


def test_a_bad_setting_stops_with_one_plain_line(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (private_overlay_is_a_temp_folder / "vault_connector.yaml").write_text(
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\npublic_url: http://vault.example.com\n",
        encoding="utf-8",
    )

    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "public_url must start with https://" in err
    assert "Traceback" not in err
