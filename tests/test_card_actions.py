import asyncio
import json

import pytest

from src.brain.followups.cards import card_to_json, parse_card
from src.brain.plugins.listeners.slack import _normalize_interactive
from src.db import get_card, get_follow_up, record_card, update_follow_up, upsert_follow_up
from src.orchestrator import card_actions, digest_actions, reply_routing

OWNER = {"name": "owner", "areas": "*", "is_owner": True}
CO_OWNER = {"name": "The Partner", "areas": ["family", "alfred"], "is_owner": False}
REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "humans": {"chat": None, "delivery": {"slack": {"channel_id": "DPRIV"}}},
}


@pytest.fixture
def env(isolated_db, monkeypatch):
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", REGISTRY)
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", REGISTRY)
    monkeypatch.setattr(digest_actions, "_local_today", lambda: "2026-10-17")

    async def no_home(notifier, *, today=None):
        return {"type": "home", "blocks": []}

    monkeypatch.setattr("src.brain.followups.home.build_home_view", no_home)
    calls: list[dict] = []

    async def fake_on_reply(msg):
        calls.append(msg)
        return {"outcome": "approved", "action_id": msg.get("action_id")} if msg["type"] == "approval" else None

    monkeypatch.setattr(reply_routing, "on_reply", fake_on_reply)
    return calls


def follow_up(place="private"):
    item = upsert_follow_up(source="slack_private", ref="r", asked_by="owner", place=place,
                            summary="Send Andrei the CV", dedupe_key="k", first_seen="2026-10-15T05:00:00")
    update_follow_up(item, status="raised")
    return item


def posted(place="private", channel="DPRIV", area="humans", **extra):
    raw = {"area": area, "emoji": "📤", "title": "Andrei CV", "line": "The CV for Andrei is still not confirmed as sent. He asked for it this week. Check sent mail and send it today if it is missing.",
           "detail": {"heading": "Send Andrei the CV"}, **extra}
    return record_card(place=place, channel=channel, summary_ts="10.1", detail_ts="10.2", area=area,
                       follow_up_id=extra.get("follow_up_id"), card=card_to_json(parse_card(raw)))


def click(action, value, *, who=OWNER, channel="DPRIV", ts="10.2"):
    msg = {"type": "card", "action": action, "value": value, "sender": "U1", "group_id": channel,
           "target_timestamp": ts, "trigger_id": "T", "timestamp": "a.1"}
    return asyncio.run(digest_actions.handle(msg, who))


def test_done_closes_the_follow_up_and_marks_both_messages(env):
    item = follow_up()
    card_id = posted(follow_up_id=item)
    result = click("card_done", str(card_id))
    assert get_follow_up(item)["status"] == "done" and get_card(card_id)["status"] == "done"
    updates = {u["ts"]: json.dumps(u["blocks"], ensure_ascii=False) for u in result["updates"]}
    assert "✅ Done" in updates["10.1"] and "✅ Done" in updates["10.2"]
    assert "card_done" not in updates["10.2"]


def test_tomorrow_and_drop_change_the_follow_up(env):
    item = follow_up()
    later = posted(follow_up_id=item)
    click("card_later", str(later))
    assert get_follow_up(item)["last_raised"] == "2026-10-18" and get_card(later)["status"] == "later"
    dropped = posted(follow_up_id=item)
    click("card_drop", str(dropped))
    assert get_follow_up(item)["status"] == "dismissed"


def test_a_click_must_come_from_the_cards_own_message(env):
    card_id = posted()
    assert click("card_done", str(card_id), ts="99.9")["ephemeral"]["text"] == card_actions.NOT_ON_MESSAGE
    assert click("card_done", str(card_id), channel="CSHARED")["ephemeral"]["text"] == card_actions.NOT_ON_MESSAGE
    assert click("card_done", "999")["ephemeral"]["text"] == card_actions.NOT_ON_MESSAGE
    assert get_card(card_id)["status"] == "open"


def test_only_the_owner_acts_on_a_private_card(env):
    card_id = posted()
    assert click("card_done", str(card_id), who=CO_OWNER)["ephemeral"]["text"] == card_actions.OWNER_ONLY
    assert get_card(card_id)["status"] == "open"


def test_the_co_owner_may_finish_a_shared_card(env):
    item = follow_up("shared")
    card_id = posted(place="shared", channel="CSHARED", area="family", follow_up_id=item)
    click("card_done", str(card_id), who=CO_OWNER, channel="CSHARED")
    assert get_card(card_id)["status"] == "done" and get_follow_up(item)["status"] == "done"


def test_approve_goes_through_the_approval_path_and_marks_the_line(env):
    card_id = posted(approvals=[{"id": "abcdef123456", "label": "Send wishes"}])
    result = click("card_approve", f"{card_id}:0")
    [approval] = env
    assert (approval["type"], approval["action_id"], approval["decision"], approval["group_id"]) == (
        "approval", "abcdef123456", "approved", "DPRIV")
    detail = next(u for u in result["updates"] if u["ts"] == "10.2")
    assert "Approved: Send wishes" in json.dumps(detail["blocks"])


def test_ask_sends_the_label_to_alfred_in_the_cards_thread(env):
    card_id = posted(asks=["Draft a cover note"])
    click("card_ask", f"{card_id}:0")
    [ask] = env
    assert ask["type"] == "reply" and ask["text"] == "Draft a cover note"
    assert (ask["quoted_timestamp"], ask["group_id"], ask["quoted_from_bot"]) == ("10.1", "DPRIV", True)
    assert ask["reply_thread_ts"] == "10.1"


def test_a_bad_index_does_nothing(env):
    card_id = posted(asks=["Draft"])
    assert click("card_ask", f"{card_id}:5") == {}
    assert env == []


def test_the_listener_turns_card_buttons_into_card_envelopes():
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "DPRIV"},
               "message": {"ts": "10.2"}, "trigger_id": "T", "action_ts": "11.1",
               "container": {"type": "message"},
               "actions": [{"action_id": "card_done", "value": "7"}]}
    envelope = _normalize_interactive(payload)
    assert (envelope["type"], envelope["action"], envelope["value"], envelope["target_timestamp"]) == (
        "card", "card_done", "7", "10.2")
