import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.brain.followups import home
from src.brain.plugins.notifiers.slack import SlackNotifier
from src.db import get_db, set_follow_up_work, update_follow_up, upsert_follow_up
from src.orchestrator import reply_routing

TODAY = date(2026, 10, 17)
HEADERS = ["Needs you", "Today", "Health", "Open loops", "Done overnight"]


@pytest.fixture
def env(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
    monkeypatch.setattr(home, "_today_lines", lambda today: ["09:00 Dentist"])
    return isolated_db


def notifier(upload=None):
    client = AsyncMock()
    client.files_upload_v2 = upload or AsyncMock(return_value={"ok": True, "files": [{"id": "F123"}]})
    client.views_publish = AsyncMock(return_value={"ok": True})
    slack = SlackNotifier(bot_token="xoxb-test", channels={"default": {"channel_id": "C_DEFAULT"}})
    slack._client = client
    return slack, client


def weights():
    conn = get_db()
    for day, kg in (("2026-10-10", 70.0), ("2026-10-12", 70.4), ("2026-10-16", 70.9)):
        conn.execute("INSERT INTO garmin_days (date, weight_kg, sleep_score, synced_at) VALUES (?, ?, 80, ?)", (day, kg, "2026-10-17T05:00:00"))
    conn.commit()


def headers(view):
    return [b["text"]["text"] for b in view["blocks"] if b["type"] == "header"]


def build(slack):
    return asyncio.run(home.build_home_view(slack, today=TODAY))


def test_the_owner_gets_the_full_view_and_others_one_line(env, monkeypatch):
    slack, _ = notifier()
    monkeypatch.setattr("src.brain.mcp._common.notifier", lambda: slack)
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    owner = asyncio.run(reply_routing.on_reply({"type": "home_opened", "sender": "UOWNER", "timestamp": ""}))
    assert owner["home"]["user"] == "UOWNER" and headers(owner["home"]["view"]) == HEADERS
    other = asyncio.run(reply_routing.on_reply({"type": "home_opened", "sender": "USTRANGER", "timestamp": ""}))
    assert "This is a private assistant." in json.dumps(other["home"]["view"])
    assert "Needs you" not in json.dumps(other["home"]["view"])


def test_the_view_shows_last_nights_items_and_today(env):
    item = upsert_follow_up(source="slack_private", ref="r", asked_by="owner", place="private",
                            summary="Book the dentist", dedupe_key="k", first_seen="2026-10-15T05:00:00")
    update_follow_up(item, status="raised")
    set_follow_up_work(item, outcome="asked", action_ids=None, night_of="2026-10-16")
    view = build(notifier()[0])
    dumped = json.dumps(view)
    assert "Book the dentist" in dumped and "09:00 Dentist" in dumped and "1 open" in dumped


def test_the_chart_is_uploaded_once_a_day(env, monkeypatch):
    weights()
    monkeypatch.setattr("src.brain.media.charts.render_chart", lambda spec: b"png")
    slack, client = notifier()
    first, second = build(slack), build(slack)
    assert client.files_upload_v2.await_count == 1
    for view in (first, second):
        assert [b["slack_file"] for b in view["blocks"] if b["type"] == "image"] == [{"id": "F123"}]


def test_a_failed_upload_still_publishes_without_an_image(env, monkeypatch):
    weights()
    monkeypatch.setattr("src.brain.media.charts.render_chart", lambda spec: b"png")
    slack, _ = notifier(upload=AsyncMock(side_effect=RuntimeError("upload refused")))
    view = build(slack)
    assert headers(view) == HEADERS and not [b for b in view["blocks"] if b["type"] == "image"]
    assert "weight 70.9 kg" in json.dumps(view)


def test_no_weight_data_means_no_chart(env):
    slack, client = notifier()
    view = build(slack)
    client.files_upload_v2.assert_not_awaited()
    assert "No Garmin data yet." in json.dumps(view)


def test_refresh_home_without_an_owner_id_does_nothing(env, monkeypatch):
    monkeypatch.delenv("OWNER_SLACK_USER_ID", raising=False)
    slack, client = notifier()
    asyncio.run(home.refresh_home(slack))
    client.views_publish.assert_not_awaited()


def test_refresh_home_publishes_to_the_owner(env, monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    slack, client = notifier()
    asyncio.run(home.refresh_home(slack))
    assert client.views_publish.await_args.kwargs["user_id"] == "UOWNER"


def test_a_refused_image_is_published_again_without_it(env):
    slack, client = notifier()
    client.views_publish = AsyncMock(side_effect=[{"ok": False, "error": "invalid_blocks"}, {"ok": True}])
    view = {"type": "home", "blocks": [{"type": "section"}, {"type": "image", "slack_file": {"id": "F1"}}]}
    assert asyncio.run(slack.publish_home(user="UOWNER", view=view))
    second = client.views_publish.await_args_list[1].kwargs["view"]
    assert [b["type"] for b in second["blocks"]] == ["section"]


def test_calendar_and_reminder_lines_are_read_plainly():
    from datetime import datetime

    start = "2026-10-17T09:00:00+02:00"
    local = datetime.fromisoformat(start).astimezone().strftime("%H:%M")
    assert home.event_line(f"{start} → 2026-10-17T10:00:00+02:00 | Home | [Dentist] | https://x") == f"{local} Dentist"
    assert home.event_line("2026-10-17 → 2026-10-18 | Home | [Our time] | https://x") == "all day Our time"
    assert home.event_line("No events between a and b.") is None
    due = "• [Errands] | title=[Buy milk] | due=2026-10-17 07:00:00 +0000 | alarms=0 | p=0 | id=[x]"
    later = due.replace("2026-10-17", "2026-10-20")
    assert home.reminder_line(due, TODAY) == "Reminder: Buy milk"
    assert home.reminder_line(later, TODAY) is None


def test_today_lists_all_day_events_first_then_times_then_reminders():
    lines = ["Reminder: Buy milk", "14:00 Swim", "all day Our time", "09:00 Dentist"]
    assert home.order_today(lines) == ["all day Our time", "09:00 Dentist", "14:00 Swim", "Reminder: Buy milk"]
