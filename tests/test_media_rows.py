from __future__ import annotations

from datetime import UTC, datetime

from src.db import (
    expire_follow_ups,
    expire_media,
    get_follow_up,
    get_media,
    list_follow_ups,
    mark_media_sent,
    record_media,
    update_follow_up,
    upsert_follow_up,
)


def test_media_row_round_trip_and_sent_mark(isolated_db):
    record_media(media_id="m-0123456789ab", agent="health", run_label="health@1", area="health",
                 place="private", kind="chart", path="media/m-0123456789ab.png", size=10, sha256="ab"*32)
    row = get_media("m-0123456789ab")
    assert row["place"] == "private" and row["sent_ts"] is None
    mark_media_sent("m-0123456789ab", channel="D1", ts="1.2")
    assert get_media("m-0123456789ab")["sent_channel"] == "D1"
    assert get_media("m-nope") is None


def test_expire_media_returns_paths_once(isolated_db):
    record_media(media_id="m-aaaaaaaaaaaa", agent="a", run_label="a@1", area=None, place="private",
                 kind="image", path="media/old.png", size=1, sha256="0"*64)
    isolated_db.get_db().execute("UPDATE media SET ts='2026-09-01T00:00:00' WHERE id='m-aaaaaaaaaaaa'")
    assert expire_media(30, now=datetime(2026, 10, 16, tzinfo=UTC)) == ["media/old.png"]
    assert expire_media(30, now=datetime(2026, 10, 16, tzinfo=UTC)) == []


def test_follow_up_upsert_dedupes_and_lists_by_status_and_since(isolated_db):
    first = upsert_follow_up(source="claude_code", ref="brain/abc.jsonl:12", asked_by="owner",
                             place="private", summary="old wording", dedupe_key="k1", first_seen="2026-10-10T05:00:00")
    again = upsert_follow_up(source="claude_code", ref="brain/abc.jsonl:12", asked_by="owner",
                             place="private", summary="new wording", dedupe_key="k1", first_seen="2026-10-16T05:00:00")
    assert first == again and first.startswith("F-") and len(first) == 8
    assert [r["summary"] for r in list_follow_ups(status="open")] == ["new wording"]
    assert update_follow_up(first, status="raised", last_raised="2026-10-16T05:10:00")
    assert list_follow_ups(status="raised", since="2026-10-16") and not list_follow_ups(status="raised", since="2026-10-17")
    assert not update_follow_up("F-999999", status="done")


def test_upserting_a_known_follow_up_refreshes_only_its_summary(isolated_db):
    same = {"source": "claude_code", "ref": "r", "asked_by": "owner", "place": "private", "dedupe_key": "k1"}
    follow_up_id = upsert_follow_up(**same, summary="old", first_seen="2026-10-10T05:00:00")
    update_follow_up(follow_up_id, status="raised")

    again = upsert_follow_up(**same, summary="new", first_seen="2026-10-16T05:00:00")

    [row] = list_follow_ups()
    assert again == follow_up_id
    assert (row["summary"], row["status"], row["first_seen"]) == ("new", "raised", "2026-10-10T05:00:00")


def test_get_follow_up_returns_the_row_or_none(isolated_db):
    follow_up_id = upsert_follow_up(source="slack_shared", ref="p", asked_by="U2", place="shared",
                                    summary="Who books the dentist?", dedupe_key="k1",
                                    first_seen="2026-10-15T07:00:00")
    row = get_follow_up(follow_up_id)
    assert (row["id"], row["place"], row["status"], row["summary"]) == (
        follow_up_id, "shared", "open", "Who books the dentist?")
    assert get_follow_up("F-999999") is None
    assert get_follow_up("") is None


def test_expire_media_keeps_a_row_made_exactly_at_the_cutoff(isolated_db):
    for media_id, ts in (("m-cccccccccccc", "2026-09-16T00:00:00"), ("m-dddddddddddd", "2026-09-15T23:59:59")):
        record_media(media_id=media_id, agent="a", run_label="a@1", area=None, place="private",
                     kind="image", path=f"media/{media_id}.png", size=1, sha256="0"*64)
        isolated_db.get_db().execute("UPDATE media SET ts=? WHERE id=?", (ts, media_id))
    assert expire_media(30, now=datetime(2026, 10, 16, tzinfo=UTC)) == ["media/m-dddddddddddd.png"]
    assert get_media("m-cccccccccccc")["deleted_at"] is None


def test_follow_up_ids_count_up_and_update_keeps_the_fields_it_was_not_given(isolated_db):
    ids = [upsert_follow_up(source="slack_private", ref=f"r{i}", asked_by="owner", place="private",
                            summary=f"s{i}", dedupe_key=f"k{i}", first_seen="2026-10-16T05:00:00")
           for i in range(3)]
    assert ids == ["F-000001", "F-000002", "F-000003"]
    assert update_follow_up(ids[0], status="raised", summary="clearer", research="found it",
                            last_raised="2026-10-16T05:10:00")
    assert update_follow_up(ids[0], status="done")
    row = list_follow_ups(status="done")[0]
    assert (row["summary"], row["research"], row["last_raised"]) == (
        "clearer", "found it", "2026-10-16T05:10:00")


def test_list_follow_ups_since_includes_an_item_raised_at_that_moment(isolated_db):
    follow_up_id = upsert_follow_up(source="claude_code", ref="r", asked_by="owner", place="private",
                                    summary="s", dedupe_key="k", first_seen="2026-10-10T05:00:00")
    update_follow_up(follow_up_id, status="raised", last_raised="2026-10-16T05:10:00")
    assert [r["id"] for r in list_follow_ups(since="2026-10-16T05:10:00")] == [follow_up_id]
    assert list_follow_ups(since="2026-10-16T05:10:01") == []


def test_expire_follow_ups_only_touches_open_and_raised_items_past_the_window(isolated_db):
    for key, seen, status in (("done", "2026-09-01T05:00:00", "done"),
                              ("dismissed", "2026-09-01T05:00:00", "dismissed"),
                              ("raised", "2026-09-01T05:00:00", "raised"),
                              ("open", "2026-09-01T05:00:00", "open"),
                              ("recent", "2026-10-15T05:00:00", "open")):
        follow_up_id = upsert_follow_up(source="claude_ai", ref=key, asked_by=None, place="private",
                                        summary=key, dedupe_key=key, first_seen=seen)
        update_follow_up(follow_up_id, status=status)
    assert expire_follow_ups(14, now=datetime(2026, 10, 16, tzinfo=UTC)) == 2
    assert {r["dedupe_key"]: r["status"] for r in list_follow_ups()} == {
        "done": "done", "dismissed": "dismissed", "raised": "expired", "open": "expired", "recent": "open"}

