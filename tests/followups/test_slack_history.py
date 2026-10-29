import asyncio
import logging
import time
from datetime import date
from types import SimpleNamespace

from slack_sdk.errors import SlackApiError

from src.brain.followups.entries import Entry
from src.brain.followups.slack_history import (
    MORNING_MESSAGE_EVENT,
    ChannelCursor,
    ChannelRole,
    SlackBudget,
    Speakers,
    channel_roles,
    read_channel,
    speakers_from,
)

CURSOR = "1791957600.000000"
WINDOW_START = "1790748000.000000"
RUN_START = "1792044000.000000"
LATER = "1792130400.000000"
SHARED = ChannelRole("C0SHARED1", "slack_shared", "shared")
PRIVATE = ChannelRole("D0PRIVATE1", "slack_private", "private")

INTEGRATIONS = {
    "notifier": {
        "type": "slack",
        "options": {
            "channels": {
                "alfred": {"channel_id": "C0SHARED1"},
                "alfred_private": {"channel_id": "D0PRIVATE1"},
                "finance": {"channel_id": "C0FINANCE1"},
            }
        },
    }
}
FOLLOWUPS = {
    "old_channels": ["C0OLDFIN1", "C0OLDHEA1"],
    "old_channels_until": date(2026, 10, 30),
}


def permalink(channel: str, ts: str) -> str:
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def msg(ts: str, text: str, user: str | None = "U1", **extra):
    return {"type": "message", "user": user, "text": text, "ts": ts, **extra}


def _page(pages, cursor):
    index = int(cursor.removeprefix("page-")) if cursor else 0
    more = index + 1 < len(pages)
    return {
        "ok": True,
        "messages": pages[index],
        "response_metadata": {"next_cursor": f"page-{index + 1}" if more else ""},
    }


class FakeSlack:
    def __init__(self, history=None, replies=None, failures=None):
        self.history = history or {}
        self.replies = replies or {}
        self.failures = failures or {}
        self.calls = []
        self.params = []

    async def conversations_history(self, *, channel, oldest, limit, cursor=None, **params):
        self.calls.append(("history", channel, oldest, limit, cursor))
        self.params.append(params)
        return _page(self.history[channel], cursor)

    async def conversations_replies(self, *, channel, ts, oldest, limit, cursor=None, **params):
        self.calls.append(("replies", channel, ts, oldest, limit, cursor))
        self.params.append(params)
        if ts in self.failures:
            raise self.failures[ts]
        return _page(self.replies[(channel, ts)], cursor)


def budget(replies=40, seconds=90.0):
    return SlackBudget(replies_left=replies, deadline=time.monotonic() + seconds)


FROM_THE_CURSOR = ChannelCursor(CURSOR)
PEOPLE = Speakers(owner="U1", co_owners=frozenset({"U2"}), bot_id="B1")


def read(client, role, cursor=FROM_THE_CURSOR, *, now=RUN_START, spend=None, who=PEOPLE):
    return asyncio.run(
        read_channel(
            client,
            role,
            cursor,
            now_ts=now,
            window_days=14,
            budget=spend or budget(),
            permalink=permalink,
            speakers=who,
        )
    )


def fetch(client, roles):
    return [entry for role in roles for entry in read(client, role).entries]


THREADS = [
    ("1791957700.000100", "1791957800.000100"),
    ("1791958700.000100", "1791958800.000100"),
    ("1791959700.000100", "1791959800.000100"),
]


def three_threads():
    history, replies = [], {}
    for number, (parent_ts, reply_ts) in enumerate(THREADS, start=1):
        parent = msg(
            parent_ts, f"question {number}", reply_count=1, latest_reply=reply_ts,
            thread_ts=parent_ts,
        )
        answer = msg(reply_ts, f"answer {number}", user="U2", thread_ts=parent_ts)
        history.insert(0, parent)
        replies[("C0SHARED1", parent_ts)] = [[parent, answer]]
    return FakeSlack(history={"C0SHARED1": [history]}, replies=replies)


def rate_limited():
    response = SimpleNamespace(status_code=429, get=lambda key, default=None: default)
    return SlackApiError("ratelimited", response)


ALL_THREE = ["question 1", "answer 1", "question 2", "answer 2", "question 3", "answer 3"]


def test_history_pages_are_followed_across_cursors_and_returned_oldest_first():
    client = FakeSlack(
        history={
            "C0SHARED1": [
                [msg("1791957900.000100", "third"), msg("1791957800.000100", "second")],
                [msg("1791957700.000100", "first")],
            ]
        }
    )
    entries = fetch(client, [SHARED])
    assert [e.text for e in entries] == ["first", "second", "third"]
    assert client.calls == [
        ("history", "C0SHARED1", WINDOW_START, 200, None),
        ("history", "C0SHARED1", WINDOW_START, 200, "page-1"),
    ]


def test_a_parent_days_before_the_cursor_yields_only_its_new_reply():
    parent_ts = "1791698400.000100"
    reply_ts = "1791961200.000100"
    parent = msg(
        parent_ts, "an old question", reply_count=1, latest_reply=reply_ts, thread_ts=parent_ts
    )
    client = FakeSlack(
        history={"C0SHARED1": [[parent]]},
        replies={
            ("C0SHARED1", parent_ts): [
                [parent, msg(reply_ts, "a new answer", user="U2", thread_ts=parent_ts)]
            ]
        },
    )
    assert [e.text for e in fetch(client, [SHARED])] == ["a new answer"]
    assert client.calls == [
        ("history", "C0SHARED1", WINDOW_START, 200, None),
        ("replies", "C0SHARED1", parent_ts, CURSOR, 200, None),
    ]


def test_a_thread_whose_last_reply_is_not_newer_than_the_cursor_is_not_fetched():
    parent_ts = "1791698400.000100"
    parent = msg(
        parent_ts,
        "an old question",
        reply_count=2,
        latest_reply="1791784800.000100",
        thread_ts=parent_ts,
    )
    client = FakeSlack(history={"C0SHARED1": [[parent]]})
    assert fetch(client, [SHARED]) == []
    assert [call[0] for call in client.calls] == ["history"]


def test_replies_are_paged_too():
    parent_ts = "1791957700.000100"
    parent = msg(parent_ts, "a question", reply_count=2, thread_ts=parent_ts)
    client = FakeSlack(
        history={"C0SHARED1": [[parent]]},
        replies={
            ("C0SHARED1", parent_ts): [
                [parent, msg("1791957800.000100", "first answer", thread_ts=parent_ts)],
                [msg("1791957900.000100", "second answer", thread_ts=parent_ts)],
            ]
        },
    )
    assert [e.text for e in fetch(client, [SHARED])] == [
        "a question",
        "first answer",
        "second answer",
    ]


def test_join_events_empty_text_and_messages_at_or_before_the_cursor_are_dropped():
    client = FakeSlack(
        history={
            "C0SHARED1": [
                [
                    msg("1791958100.000100", "<@U3> has joined", user="U3", subtype="channel_join"),
                    msg("1791958000.000100", "   "),
                    msg(CURSOR, "right at the cursor"),
                    msg("1791957599.999999", "before the cursor"),
                    msg("1791957700.000100", "kept"),
                ]
            ]
        }
    )
    assert [e.text for e in fetch(client, [SHARED])] == ["kept"]


def test_messages_after_the_run_start_are_left_for_the_next_run():
    parent_ts = "1791957700.000100"
    parent = msg(parent_ts, "a question", reply_count=2, thread_ts=parent_ts)
    client = FakeSlack(
        history={"C0SHARED1": [[msg("1792044000.000001", "just after the start"), parent]]},
        replies={
            ("C0SHARED1", parent_ts): [
                [
                    parent,
                    msg(RUN_START, "right at the start", thread_ts=parent_ts),
                    msg("1792044000.500000", "a reply after the start", thread_ts=parent_ts),
                ]
            ]
        },
    )
    assert [e.text for e in fetch(client, [SHARED])] == ["a question", "right at the start"]


def test_people_and_bots_are_named_by_who_they_are():
    client = FakeSlack(
        history={
            "D0PRIVATE1": [
                [
                    msg("1791958200.000100", "added an app", user="U9", bot_id="B1", subtype="bot_add"),
                    msg("1791958100.000100", "Deploy finished", user=None, bot_id="B2"),
                    msg(
                        "1791958000.000100",
                        "Morning summary",
                        user=None,
                        subtype="bot_message",
                        bot_id="B1",
                    ),
                    msg("1791957950.000100", "On it.", user="U_BOT", bot_id="B1"),
                    msg("1791957900.000100", "a guest says hi", user="U3"),
                    msg("1791957800.000100", "Who books the dentist?", user="U2"),
                    msg("1791957700.000100", "Can you move it to Friday?", user="U1"),
                ]
            ]
        }
    )
    assert [(e.who, e.text) for e in fetch(client, [PRIVATE])] == [
        ("owner", "Can you move it to Friday?"),
        ("co-owner", "Who books the dentist?"),
        ("user:U3", "a guest says hi"),
        ("alfred", "On it."),
        ("alfred", "Morning summary"),
        ("bot:B2", "Deploy finished"),
    ]


def test_without_our_bot_id_a_bot_is_named_by_its_own_id():
    client = FakeSlack(history={"D0PRIVATE1": [[msg("1791957950.000100", "On it.", bot_id="B1")]]})
    assert [e.who for e in read(client, PRIVATE, who=Speakers()).entries] == ["bot:B1"]


def test_speakers_come_from_the_owner_id_and_the_authorized_senders():
    integrations = {
        "notifier": {"options": {"authorized_senders": {"U2": {"areas": []}, "U1": {}}}}
    }
    assert speakers_from(integrations, owner="U1", bot_id="B1") == PEOPLE
    assert speakers_from({"notifier": "noop"}, owner=None, bot_id=None) == Speakers()


def test_a_persons_message_with_a_file_keeps_its_text_and_drops_the_file():
    file = {"id": "F1", "name": "photo.jpg", "url_private": "https://files.slack.com/x"}
    client = FakeSlack(
        history={
            "C0SHARED1": [
                [
                    msg("1791957800.000100", "", subtype="file_share", files=[file]),
                    msg(
                        "1791957700.000100",
                        "can you add this to the calendar?",
                        subtype="file_share",
                        files=[file],
                    ),
                ]
            ]
        }
    )
    entries = fetch(client, [SHARED])
    assert [(e.who, e.text) for e in entries] == [("owner", "can you add this to the calendar?")]


def test_a_reply_carries_the_text_of_the_message_its_thread_answers():
    parent_ts = "1791698400.000100"
    reply_ts = "1791961200.000100"
    question = "Who books the dentist for the kids this autumn? " * 4
    parent = msg(parent_ts, question, reply_count=2, latest_reply=reply_ts, thread_ts=parent_ts)
    broadcast = msg(
        "1791961100.000100", "I can on Friday", user="U2", thread_ts=parent_ts,
        subtype="thread_broadcast",
    )
    client = FakeSlack(
        history={"C0SHARED1": [[broadcast, parent]]},
        replies={
            ("C0SHARED1", parent_ts): [
                [parent, broadcast, msg(reply_ts, "booked", thread_ts=parent_ts)]
            ]
        },
    )
    entries = fetch(client, [SHARED])
    assert [(e.text, e.thread) for e in entries] == [
        ("I can on Friday", question.strip()),
        ("booked", question.strip()),
    ]


def test_a_message_that_starts_a_thread_carries_no_thread():
    parent_ts = "1791957700.000100"
    parent = msg(parent_ts, "a question", reply_count=1, thread_ts=parent_ts)
    client = FakeSlack(
        history={"C0SHARED1": [[parent]]},
        replies={("C0SHARED1", parent_ts): [[parent, msg("1791957800.000100", "an answer", thread_ts=parent_ts)]]},
    )
    assert [(e.text, e.thread) for e in fetch(client, [SHARED])] == [
        ("a question", None),
        ("an answer", "a question"),
    ]


def test_the_bots_own_morning_messages_are_left_out_and_replies_to_them_kept():
    morning_ts = "1791957700.000100"
    morning = msg(
        morning_ts, "F-000012 Did the Garmin sync get fixed?", user="U_BOT", bot_id="B1",
        reply_count=1, thread_ts=morning_ts,
        metadata={"event_type": MORNING_MESSAGE_EVENT, "event_payload": {}},
    )
    client = FakeSlack(
        history={"D0PRIVATE1": [[morning]]},
        replies={
            ("D0PRIVATE1", morning_ts): [
                [morning, msg("1791957800.000100", "still broken", thread_ts=morning_ts)]
            ]
        },
    )
    entries = fetch(client, [PRIVATE])
    assert [(e.who, e.text, e.thread) for e in entries] == [
        ("owner", "still broken", "F-000012 Did the Garmin sync get fixed?"),
    ]
    assert client.params and all(params.get("include_all_metadata") is True for params in client.params)


def test_a_broadcast_reply_seen_in_history_and_in_its_thread_is_kept_once():
    parent_ts = "1791957700.000100"
    reply_ts = "1791957800.000100"
    parent = msg(parent_ts, "Question?", reply_count=1, thread_ts=parent_ts)
    broadcast = msg(
        reply_ts, "Answer", user="U2", thread_ts=parent_ts, subtype="thread_broadcast"
    )
    client = FakeSlack(
        history={"C0SHARED1": [[broadcast, parent]]},
        replies={("C0SHARED1", parent_ts): [[parent, broadcast]]},
    )
    assert [e.text for e in fetch(client, [SHARED])] == ["Question?", "Answer"]


def test_each_entry_takes_source_place_ref_and_utc_time_from_its_message_and_role():
    client = FakeSlack(
        history={
            "C0SHARED1": [[msg("1791957700.123456", "hello", user="U9")]],
            "D0PRIVATE1": [[msg("1791957800.000200", "hi there", user="U1")]],
        }
    )
    assert fetch(client, [SHARED, PRIVATE]) == [
        Entry(
            source="slack_shared",
            ref="https://slack.com/archives/C0SHARED1/p1791957700123456",
            who="user:U9",
            when="2026-10-14T06:01:40.123Z",
            text="hello",
            place="shared",
        ),
        Entry(
            source="slack_private",
            ref="https://slack.com/archives/D0PRIVATE1/p1791957800000200",
            who="owner",
            when="2026-10-14T06:03:20.000Z",
            text="hi there",
            place="private",
        ),
    ]


def _summary(roles):
    return [(r.channel_id, r.source, r.place) for r in roles]


def test_channel_roles_name_the_two_places_and_the_old_channels_through_their_last_day():
    roles = channel_roles(INTEGRATIONS, FOLLOWUPS, today=date(2026, 10, 30))
    assert _summary(roles) == [
        ("C0SHARED1", "slack_shared", "shared"),
        ("D0PRIVATE1", "slack_private", "private"),
        ("C0OLDFIN1", "slack_old", "private"),
        ("C0OLDHEA1", "slack_old", "private"),
    ]


def test_channel_roles_drop_the_old_channels_the_day_after_their_last_day():
    roles = channel_roles(INTEGRATIONS, FOLLOWUPS, today=date(2026, 10, 31))
    assert _summary(roles) == [
        ("C0SHARED1", "slack_shared", "shared"),
        ("D0PRIVATE1", "slack_private", "private"),
    ]


def test_channel_roles_are_empty_when_slack_is_not_configured():
    assert channel_roles({"notifier": "noop", "tasks": "noop"}, {}, today=date(2026, 10, 16)) == []


def test_a_channel_read_to_the_end_moves_its_cursor_to_the_run_start():
    result = read(three_threads(), SHARED)
    assert [e.text for e in result.entries] == ALL_THREE
    assert result.cursor == ChannelCursor(RUN_START) and result.finished


def test_replies_stop_at_the_budget_and_the_next_run_goes_on_after_the_last_done_message():
    client = three_threads()
    first = read(client, SHARED, spend=budget(replies=2))
    assert [e.text for e in first.entries] == ALL_THREE[:4]
    assert first.cursor == ChannelCursor(CURSOR, until=RUN_START, reached=THREADS[1][0])
    assert not first.finished
    client.calls.clear()
    second = read(client, SHARED, first.cursor, now=LATER)
    assert [e.text for e in second.entries] == ALL_THREE[4:]
    assert second.cursor == ChannelCursor(LATER) and second.finished
    assert client.calls[0] == ("history", "C0SHARED1", THREADS[1][0], 200, None)
    assert [call[2] for call in client.calls if call[0] == "replies"] == [THREADS[2][0]]


def test_a_pass_stopped_at_its_first_thread_leaves_the_cursor_as_it_was():
    client = three_threads()
    first = read(client, SHARED, spend=budget(replies=0))
    assert (first.entries, first.cursor, first.finished) == ([], ChannelCursor(CURSOR), False)
    second = read(client, SHARED, first.cursor, now=LATER)
    assert [e.text for e in second.entries] == ALL_THREE


def test_a_thread_that_fails_is_logged_and_skipped_and_the_channel_goes_on(caplog):
    client = three_threads()
    client.failures[THREADS[0][0]] = RuntimeError("thread_not_found")
    with caplog.at_level(logging.ERROR):
        result = read(client, SHARED)
    assert [e.text for e in result.entries] == ["question 1", *ALL_THREE[2:]]
    assert result.cursor == ChannelCursor(RUN_START)
    assert "C0SHARED1" in caplog.text


def test_a_rate_limited_call_stops_slack_for_the_rest_of_the_run():
    client = three_threads()
    client.failures[THREADS[1][0]] = rate_limited()
    spend = budget()
    result = read(client, SHARED, spend=spend)
    assert [e.text for e in result.entries] == ALL_THREE[:2]
    assert result.cursor == ChannelCursor(CURSOR, until=RUN_START, reached=THREADS[0][0])
    other = FakeSlack(history={"D0PRIVATE1": [[msg("1791957700.000100", "hello")]]})
    untouched = read(other, PRIVATE, spend=spend)
    assert (untouched.entries, untouched.cursor, untouched.finished) == ([], ChannelCursor(CURSOR), False)
    assert other.calls == []


def test_when_the_time_is_up_the_channel_stops_where_it_was():
    now = [0.0]
    spend = SlackBudget(replies_left=40, deadline=90.0, clock=lambda: now[0])
    client = three_threads()
    replies = client.conversations_replies

    async def slow_replies(**params):
        now[0] += 50
        return await replies(**params)

    client.conversations_replies = slow_replies
    result = read(client, SHARED, spend=spend)
    assert [e.text for e in result.entries] == ALL_THREE[:4]
    assert result.cursor.reached == THREADS[1][0]


def test_a_call_that_outlasts_the_time_left_is_cut_off():
    spend = budget(seconds=0.05)
    client = three_threads()

    async def stuck(**params):
        await asyncio.sleep(5)

    client.conversations_replies = stuck
    started = time.monotonic()
    result = read(client, SHARED, spend=spend)
    assert time.monotonic() - started < 2
    assert result.entries == [] and result.cursor == ChannelCursor(CURSOR)
    assert spend.exhausted


def test_a_pass_under_way_that_stops_again_before_a_new_message_keeps_its_cursor():
    under_way = ChannelCursor(CURSOR, until=RUN_START, reached=THREADS[0][0])
    result = read(three_threads(), SHARED, under_way, now=LATER, spend=budget(replies=0))
    assert (result.entries, result.cursor, result.finished) == ([], under_way, False)
