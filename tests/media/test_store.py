from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat

import pytest

from src.brain.media import store
from src.brain.media.store import (
    MAX_MEDIA_IDS,
    delete_expired,
    mark_sent,
    media_dir,
    media_for_place,
    parse_media_ids,
    save_png,
)
from src.db import expire_media, get_db, get_media, record_media


def events_for(actor):
    cursor = get_db().execute(
        "SELECT actor, action, subject, detail FROM events WHERE actor=? ORDER BY id", (actor,),
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _age(isolated_db, media_id):
    isolated_db.get_db().execute("UPDATE media SET ts='2026-01-01T00:00:00' WHERE id=?", (media_id,))


def test_save_png_writes_a_private_file_and_a_row(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    mid = save_png(b"\x89PNG\r\n\x1a\nxx", kind="chart", agent="health", run_label="health@1", area="health", place="private")
    assert re.fullmatch(r"m-[0-9a-f]{12}", mid)
    path = tmp_path / "media" / f"{mid}.png"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "media").stat().st_mode) == 0o700
    assert get_media(mid)["sha256"] == hashlib.sha256(b"\x89PNG\r\n\x1a\nxx").hexdigest()
    assert [e["action"] for e in events_for("media:health")] == ["media_rendered"]


def test_the_render_event_records_the_id_kind_size_and_place(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    mid = save_png(b"12345", kind="chart", agent="health", run_label="health@1", area="health", place="private")
    [event] = events_for("media:health")
    assert event["subject"] == mid
    assert json.loads(event["detail"]) == {"kind": "chart", "bytes": 5, "place": "private"}


def test_a_failed_row_insert_leaves_no_file_behind(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "record_media", locked)
    with pytest.raises(sqlite3.OperationalError):
        save_png(b"png", kind="chart", agent="a", run_label="a@1", area=None, place="private")
    assert list((tmp_path / "media").iterdir()) == []
    assert events_for("media:a") == []


def test_delete_expired_only_touches_the_current_media_folder(isolated_db, tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("USER_DIR", str(first))
    mid = save_png(b"png", kind="image", agent="a", run_label="a@1", area=None, place="shared")
    _age(isolated_db, mid)
    monkeypatch.setenv("USER_DIR", str(second))
    assert delete_expired(30) == 1
    assert (first / "media" / f"{mid}.png").exists()


def test_delete_expired_never_unlinks_outside_the_media_folder(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    keep = tmp_path / "keep.txt"
    keep.write_text("keep")
    record_media(media_id="m-0123456789ab", agent="a", run_label="a@1", area=None, place="shared",
                 kind="image", path="../keep.txt", size=4, sha256="0" * 64)
    _age(isolated_db, "m-0123456789ab")
    assert delete_expired(30) == 1
    assert keep.read_text() == "keep"


@pytest.mark.parametrize(("media_id", "file"), [
    ("m-ABCDEF123456", "media/m-ABCDEF123456.png"),
    ("m-0123456789abc", "media/m-0123456789abc.png"),
    ("../outside", "outside.png"),
])
def test_media_for_place_only_answers_for_well_formed_ids(
    isolated_db, tmp_path, monkeypatch, media_id, file,
):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    media_dir()
    (tmp_path / file).write_bytes(b"png")
    record_media(media_id=media_id, agent="a", run_label="a@1", area=None, place="shared",
                 kind="image", path=file, size=3, sha256="0" * 64)
    assert media_for_place([media_id], "shared") == ([], [media_id])


def test_media_dir_falls_back_to_the_default_overlay_when_user_dir_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_DIR", raising=False)
    monkeypatch.setattr(store, "REPO_ROOT", tmp_path)
    assert media_dir() == tmp_path / "user" / "media"
    assert media_dir(tmp_path / "other") == tmp_path / "other" / "media"
    assert stat.S_IMODE((tmp_path / "user" / "media").stat().st_mode) == 0o700


@pytest.mark.parametrize("value, expected", [
    (None, []),
    ([], []),
    (["m-0123456789ab"], ["m-0123456789ab"]),
    (["m-0123456789ab", "m-ba9876543210", "m-0123456789ab"], ["m-0123456789ab", "m-ba9876543210"]),
])
def test_parse_media_ids_keeps_order_and_drops_repeats(value, expected):
    assert parse_media_ids(value) == expected


@pytest.mark.parametrize("value, message", [
    ("m-0123456789ab", "list"),
    ([7], "list"),
    ([None], "list"),
    (["m-0123456789ab", {"id": 1}], "list"),
    (["not-an-id"], "m-0123456789ab"),
    (["m-ABCDEF123456"], "m-0123456789ab"),
    (["m-0123456789ab\n"], "m-0123456789ab"),
    (["../etc/passwd"], "m-0123456789ab"),
    ([f"m-{i:012x}" for i in range(MAX_MEDIA_IDS + 1)], "at most 4 media ids"),
    (["m-0123456789ab"] * (MAX_MEDIA_IDS + 1), "at most 4 media ids"),
])
def test_parse_media_ids_refuses_what_is_not_a_short_list_of_media_ids(value, message):
    with pytest.raises(ValueError, match=message):
        parse_media_ids(value)


def _media(isolated_db, tmp_path, monkeypatch, place, agent="health"):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    return save_png(b"png", kind="chart", agent=agent, run_label=f"{agent}@1", area=agent, place=place)


def test_media_for_place_separates_what_may_be_posted_from_what_may_not(isolated_db, tmp_path, monkeypatch):
    mine = _media(isolated_db, tmp_path, monkeypatch, "private")
    other_place = _media(isolated_db, tmp_path, monkeypatch, "shared")
    expired = _media(isolated_db, tmp_path, monkeypatch, "private")
    _age(isolated_db, expired)
    expire_media(30)
    gone = _media(isolated_db, tmp_path, monkeypatch, "private")
    (tmp_path / "media" / f"{gone}.png").unlink()
    second = _media(isolated_db, tmp_path, monkeypatch, "private")
    unknown = "m-000000000000"

    accepted, refused = media_for_place(
        [second, other_place, mine, unknown, expired, gone, second, "../x"], "private",
    )

    assert accepted == [(second, tmp_path / "media" / f"{second}.png"), (mine, tmp_path / "media" / f"{mine}.png")]
    assert refused == [other_place, unknown, expired, gone, "../x"]


def test_media_for_place_posts_nothing_when_the_place_is_unknown(isolated_db, tmp_path, monkeypatch):
    mine = _media(isolated_db, tmp_path, monkeypatch, "private")
    assert media_for_place([mine], None) == ([], [mine])


def test_mark_sent_records_where_and_when_and_logs_an_event(isolated_db, tmp_path, monkeypatch):
    mid = _media(isolated_db, tmp_path, monkeypatch, "private")
    mark_sent(mid, agent="alfred_private", channel="D1", ts="1.2")
    row = get_media(mid)
    assert (row["sent_channel"], row["sent_ts"]) == ("D1", "1.2")
    [event] = events_for("media:alfred_private")
    assert (event["action"], event["subject"]) == ("media_sent", mid)
    assert json.loads(event["detail"]) == {"channel": "D1", "place": "private", "bytes": 3}


def test_delete_expired_does_not_complain_about_a_file_that_is_already_gone(
    isolated_db, tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    mid = save_png(b"png", kind="image", agent="a", run_label="a@1", area=None, place="shared")
    _age(isolated_db, mid)
    (tmp_path / "media" / f"{mid}.png").unlink()

    with caplog.at_level("WARNING"):
        assert delete_expired(30) == 1

    assert caplog.records == []
