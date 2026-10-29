import asyncio
import json

import pytest

from src.brain.actions import queue
from src.brain.actions.types import Action, ActionType
from src.brain.mcp.caller_context import ToolCaller, caller_scope
from src.brain.mcp.tools.morning_cards import UNLINKED_AREAS, send_morning_cards
from src.db import get_card, update_follow_up, upsert_follow_up

REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "humans": {"chat": None, "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "discovery": {"chat": None, "delivery": {"slack": {"channel_id": "DPRIV"}}},
}
CLOSING_RUN = ToolCaller(agent="alfred_private", area="alfred_private")
OVERVIEW = {"line": "Saturday, 17 Oct. 2 things need you.", "detail": {"bullets": ["11:00 Music class"]}}


class FakeNotifier:
    def __init__(self):
        self.posts: list[dict] = []

    async def post_blocks(self, *, channel, text, blocks, thread_ts=None, metadata=None):
        self.posts.append({"channel": channel, "text": text, "blocks": blocks,
                           "thread_ts": thread_ts, "metadata": metadata})
        return f"{len(self.posts)}.0"

    async def post_media(self, media_ids, **kwargs):
        return {}


@pytest.fixture
def slack(isolated_db, monkeypatch):
    monkeypatch.setenv("BRAIN_VERIFY_RUN", "0")
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", REGISTRY)

    async def agenda():
        return ("22:00 Our time",)

    monkeypatch.setattr("src.brain.mcp.tools.morning_cards._today_agenda", agenda)
    notifier = FakeNotifier()
    monkeypatch.setattr("src.brain.mcp._common.notifier", lambda: notifier)
    return notifier


def send(place, cards, overview=OVERVIEW, caller=CLOSING_RUN):
    with caller_scope(caller):
        return asyncio.run(send_morning_cards({"place": place, "overview": overview, "cards": cards}))


def text(result):
    return result["content"][0]["text"]


_ITEMS = iter(range(1, 10_000))


def card(area="humans", **extra):
    if "follow_up_id" not in extra and area not in UNLINKED_AREAS:
        extra["follow_up_id"] = follow_up("shared" if area == "family" else "private", key=f"card-{next(_ITEMS)}")
    return {"area": area, "emoji": "🎂", "title": "Anjali Rao", "line": "Anjali Rao turns 40 on Wednesday. I have no notes on her yet. Tap Draft wishes and I'll write them.",
            "detail": {"heading": "Anjali · Wed 21 Oct", "bullets": ["Alex · Thu 22"]}, **extra}


def follow_up(place="private", key="k"):
    item = upsert_follow_up(source="slack_private", ref=key, asked_by="owner", place=place,
                            summary="s", dedupe_key=key, first_seen="2026-10-16T05:00:00")
    update_follow_up(item, status="raised")
    return item


def test_each_card_is_a_message_with_its_detail_and_buttons_in_the_thread(slack):
    result = send("private", [card(), card(title="Andrei CV", emoji="📤")])
    assert not result.get("is_error"), text(result)
    posts = slack.posts
    assert len(posts) == 6
    overview, overview_detail, first, first_detail, second, second_detail = posts
    assert overview["thread_ts"] is None and overview_detail["thread_ts"] == "1.0"
    assert first["thread_ts"] is None and first_detail["thread_ts"] == "3.0"
    assert first["blocks"][0]["text"]["text"] == "🎂 *Anjali Rao*\nAnjali Rao turns 40 on Wednesday. I have no notes on her yet. Tap Draft wishes and I'll write them."
    assert [b["type"] for b in first_detail["blocks"]][-1] == "actions"
    assert all(p["channel"] == "DPRIV" for p in posts)
    assert all((p["metadata"] or {}).get("event_type") == "morning_message" for p in posts)
    stored = get_card(int(first_detail["blocks"][-1]["block_id"].removeprefix("card_")))
    assert (stored["summary_ts"], stored["detail_ts"], stored["area"]) == ("3.0", "4.0", "humans")


def test_a_reply_in_a_card_thread_routes_to_its_area(slack, isolated_db):
    send("private", [card()])
    rows = isolated_db.get_db().execute(
        "SELECT area, signal_timestamp FROM notifications ORDER BY rowid").fetchall()
    assert ("humans", 3.0) in [(a, float(t)) for a, t in rows]


def test_a_card_from_the_other_place_sends_nothing(slack):
    result = send("shared", [card(area="humans")])
    assert result["is_error"] and "humans" in text(result)
    assert slack.posts == []


def test_a_private_follow_up_cannot_go_in_the_shared_channel(slack):
    private = follow_up("private")
    result = send("shared", [card(area="family", follow_up_id=private)])
    assert result["is_error"] and private in text(result)
    result = send("shared", [card(area="family", line=f"about {private}")])
    assert result["is_error"]
    assert slack.posts == []


def test_a_card_whose_text_names_a_follow_up_id_is_refused_in_either_place(slack):
    item = follow_up("private")
    named = card(detail={"bullets": [f"Home chart: steps or sleep ({item})"]})
    result = send("private", [named])
    assert result["is_error"] and "follow-up id" in text(result)
    assert slack.posts == []


def test_a_follow_up_card_carries_its_id(slack):
    item = follow_up("private")
    send("private", [card(follow_up_id=item)])
    card_id = int(slack.posts[3]["blocks"][-1]["block_id"].removeprefix("card_"))
    assert get_card(card_id)["follow_up_id"] == item


def test_approvals_that_are_not_pending_or_from_another_area_are_dropped(slack):
    queue.insert(Action(id="abcdef123456", type=ActionType.PUSH_NOTIFICATION, payload={"title": "t", "message": "m"},
                        rationale="r", origin_agent="humans", origin_area="humans"))
    queue.insert(Action(id="123456abcdef", type=ActionType.PUSH_NOTIFICATION, payload={"title": "t", "message": "m2"},
                        rationale="r", origin_agent="health", origin_area="health"))
    result = send("private", [card(approvals=[{"id": "abcdef123456", "label": "Send"},
                                              {"id": "123456abcdef", "label": "Other"}])])
    buttons = [e["text"]["text"] for e in slack.posts[3]["blocks"][-1]["elements"]]
    assert "Send" in buttons and "Other" not in buttons
    assert "123456abcdef" in text(result)


def test_only_the_closing_run_posts_cards(slack):
    chat = ToolCaller(agent="alfred_private", area="alfred_private", is_reply=True)
    assert send("private", [card()], caller=chat)["is_error"]
    assert slack.posts == []


def test_a_verification_run_writes_the_cards_to_a_file(slack, monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_VERIFY_RUN", "1")
    monkeypatch.setattr("src.orchestrator.config.MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    result = send("private", [card()])
    assert slack.posts == [] and "nothing was sent" in text(result)
    written = (tmp_path / "morning-private.md").read_text()
    assert "🎂 *Anjali Rao*\nAnjali Rao turns 40 on Wednesday. I have no notes on her yet. Tap Draft wishes and I'll write them." in written and "Alex" in written


def test_a_bad_card_sends_nothing_and_names_the_field(slack):
    result = send("private", [card(title="")])
    assert result["is_error"] and "title" in text(result)
    assert slack.posts == []



def handled(caller=CLOSING_RUN):
    from src.brain.mcp.tools.morning_cards import handled_cards

    with caller_scope(caller):
        return asyncio.run(handled_cards({}))


def _card_rows():
    from src.db import get_db

    return get_db().execute("SELECT id FROM morning_cards ORDER BY id").fetchall()


def _press(card_id, status, created=None):
    from src.db import get_db, set_card_status

    set_card_status(card_id, status)
    if created:
        get_db().execute("UPDATE morning_cards SET created=? WHERE id=?", (created, card_id))
        get_db().commit()


def test_handled_cards_lists_done_and_dropped_but_not_open_or_the_overview(slack):
    cv_line = "The CV for Andrei is not confirmed as sent. He asked for it on Tuesday. Check sent mail today."
    mail_line = "A job alert from a recruiter is in the inbox and is not in your pipeline. It adds nothing today. Archive it?"
    send("private", [card(title="Andrei CV", line=cv_line),
                     card(title="Weigh-in"),
                     card(title="Mail", line=mail_line)])
    overview, cv, weigh, mail = (row[0] for row in _card_rows())
    _press(cv, "done")
    _press(mail, "dropped")
    _press(overview, "done")

    listing = text(handled())

    assert f"Done | Andrei CV: {cv_line}" in listing
    assert f"Dropped | Mail: {mail_line}" in listing
    assert "Weigh-in" not in listing and "Saturday" not in listing


def test_tomorrow_holds_a_card_back_only_on_the_day_it_was_pressed(slack):
    send("private", [card(title="Today's put-off"), card(title="Yesterday's put-off")])
    _, today, yesterday = (row[0] for row in _card_rows())
    _press(today, "later")
    _press(yesterday, "later", created="2000-01-01T00:00:00")

    listing = text(handled())

    assert "Tomorrow | Today's put-off" in listing
    assert "Yesterday's put-off" not in listing


def test_handled_cards_forgets_answers_older_than_two_weeks(slack):
    send("private", [card(title="Old news")])
    _, old = (row[0] for row in _card_rows())
    _press(old, "done", created="2000-01-01T00:00:00")

    assert text(handled()) == "No cards were marked Done, Tomorrow or Drop."


def test_only_the_closing_run_reads_handled_cards(slack):
    chat = ToolCaller(agent="alfred_private", area="alfred_private", is_reply=True)
    area_run = ToolCaller(agent="family", area="family")
    assert handled(chat)["is_error"] and handled(area_run)["is_error"]


def test_a_card_about_something_open_needs_a_to_do_item(slack):
    result = send("private", [card(follow_up_id=None)])
    assert result["is_error"] and "follow_ups_open" in text(result)
    assert slack.posts == []


def test_news_and_the_notes_card_go_without_an_item(slack):
    news = card(area="discovery", emoji="🗞️", title="Worth knowing")
    notes = card(area="alfred_private", emoji="📝", title="Notes I changed")
    result = send("private", [news, notes], )
    assert not result.get("is_error"), text(result)


def test_the_private_overview_starts_with_the_agenda_the_code_built(slack):
    send("private", [card()])
    overview_detail = slack.posts[1]
    text = json.dumps(overview_detail["blocks"], ensure_ascii=False)
    assert text.index("22:00 Our time") < text.index("11:00 Music class")


def test_the_shared_overview_gets_no_code_built_agenda(slack):
    send("shared", [card(area="family")])
    assert "22:00 Our time" not in json.dumps(slack.posts[1]["blocks"], ensure_ascii=False)


def test_the_agenda_lists_all_day_first_then_by_time_and_says_when_a_source_fails(monkeypatch):
    from src.brain.mcp.tools import icloud_read, morning_cards

    monkeypatch.setattr(icloud_read, "today_events", lambda day: [("22:00", "Our time"), ("all day", "Holiday")])
    monkeypatch.setattr(icloud_read, "reminders_due_on", lambda day: [("10:00", "Re-render the CV")])
    assert asyncio.run(morning_cards._today_agenda()) == (
        "all day Holiday", "10:00 Reminder: Re-render the CV", "22:00 Our time",
    )

    def broken(day):
        raise OSError("no network")

    monkeypatch.setattr(icloud_read, "today_events", broken)
    assert "Calendar could not be read" in asyncio.run(morning_cards._today_agenda())
