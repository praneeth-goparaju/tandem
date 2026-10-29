from __future__ import annotations

import runpy
import sys
from datetime import UTC, datetime, timedelta

from src.brain.media import expire
from src.brain.media.store import save_png
from src.db import get_media


def _save():
    return save_png(b"png", kind="chart", agent="health", run_label="health@1", area="health", place="private")


def _age(isolated_db, media_id, days):
    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    isolated_db.get_db().execute("UPDATE media SET ts=? WHERE id=?", (stamp, media_id))


def test_main_expires_old_media_keeps_the_rest_and_prints_how_many(isolated_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    old, recent, fresh = _save(), _save(), _save()
    _age(isolated_db, old, 31)
    _age(isolated_db, recent, 29)

    expire.main()

    assert capsys.readouterr().out.strip() == "1"
    assert not (tmp_path / "media" / f"{old}.png").exists()
    assert (tmp_path / "media" / f"{recent}.png").exists() and (tmp_path / "media" / f"{fresh}.png").exists()
    assert get_media(old)["deleted_at"]
    assert not get_media(recent)["deleted_at"] and not get_media(fresh)["deleted_at"]


def test_main_prints_zero_when_nothing_is_old(isolated_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    _save()

    expire.main()

    assert capsys.readouterr().out.strip() == "0"


def test_main_does_not_count_a_row_twice(isolated_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    _age(isolated_db, _save(), 40)

    expire.main()
    expire.main()

    assert capsys.readouterr().out.split() == ["1", "0"]


def test_the_module_run_as_a_script_expires_too(isolated_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    old = _save()
    _age(isolated_db, old, 31)
    monkeypatch.delitem(sys.modules, "src.brain.media.expire")

    runpy.run_module("src.brain.media.expire", run_name="__main__")

    assert capsys.readouterr().out.strip() == "1"
    assert get_media(old)["deleted_at"]
