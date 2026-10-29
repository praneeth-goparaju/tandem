from src.db import (
    LATEST_SCHEMA_VERSION,
    dismiss_follow_up,
    get_db,
    get_follow_up,
    night_items,
    raise_follow_up,
    set_follow_up_work,
    snooze_follow_up,
    update_follow_up,
    upsert_follow_up,
)

NIGHT = "2026-10-16"


def _item(n: int) -> str:
    return upsert_follow_up(
        source="slack_private", ref=f"ref-{n}", asked_by="owner", place="private",
        summary=f"item {n}", dedupe_key=f"key-{n}", first_seen=f"2026-10-16T0{n % 10}:00:00",
    )


def test_migration_18_adds_the_night_columns_and_digest_table(isolated_db):
    columns = {row[1] for row in get_db().execute("PRAGMA table_info(follow_ups)")}
    assert {"outcome", "action_ids", "dismiss_count", "night_of"} <= columns
    tables = {row[0] for row in get_db().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "digest_messages" in tables
    assert LATEST_SCHEMA_VERSION >= 18


def test_set_follow_up_work_round_trips_through_night_items(isolated_db):
    item = _item(1)
    assert set_follow_up_work(item, outcome="prepared", action_ids=["abcdef123456"], night_of=NIGHT)
    [row] = night_items(NIGHT)
    assert (row["id"], row["outcome"], row["action_ids"]) == (item, "prepared", ["abcdef123456"])
    assert not set_follow_up_work("F-999999", outcome="found", action_ids=None, night_of=NIGHT)


def test_raising_has_no_daily_limit(isolated_db):
    items = [_item(n) for n in range(6)]
    assert all(raise_follow_up(item, raised_at="2026-10-16T23:10:00") for item in items)


def test_an_item_dismissed_twice_is_never_raised_again(isolated_db):
    item = _item(1)
    assert dismiss_follow_up(item)
    assert update_follow_up(item, status="open")
    assert dismiss_follow_up(item)
    assert update_follow_up(item, status="open")
    assert not raise_follow_up(item, raised_at="2026-10-16T23:10:00")
    assert get_follow_up(item)["dismiss_count"] == 2


def test_dismiss_only_from_open_or_raised(isolated_db):
    item = _item(1)
    update_follow_up(item, status="done")
    assert not dismiss_follow_up(item)
    assert get_follow_up(item)["dismiss_count"] == 0


def test_snooze_reopens_until_a_day(isolated_db):
    item = _item(1)
    raise_follow_up(item, raised_at="2026-10-16T23:10:00")
    assert snooze_follow_up(item, until="2026-10-18")
    row = get_follow_up(item)
    assert (row["status"], row["last_raised"]) == ("raised", "2026-10-18")
