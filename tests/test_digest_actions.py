import asyncio
import json

import pytest

from src.brain.plugins.listeners.slack import _normalize_interactive
from src.db import set_follow_up_work, update_follow_up, upsert_follow_up
from src.orchestrator import digest_actions

OWNER = {"name": "owner", "areas": "*", "is_owner": True}
CO_OWNER = {"name": "The Partner", "areas": ["family"], "is_owner": False}
NIGHT = "2026-10-16"


@pytest.fixture
def env(isolated_db, monkeypatch):
    rows: dict[str, dict] = {}
    monkeypatch.setattr(digest_actions.queue, "get", rows.get)
    monkeypatch.setattr(digest_actions, "_local_today", lambda: "2026-10-17")

    async def no_home(notifier, *, today=None):
        return {"type": "home", "blocks": []}

    monkeypatch.setattr("src.brain.followups.home.build_home_view", no_home)
    return rows


def worked(n: int, *, action_ids=None) -> str:
    item = upsert_follow_up(
        source="slack_private", ref=f"ref-{n}", asked_by="owner", place="private",
        summary=f"Loop {n}", dedupe_key=f"key-{n}", first_seen=f"2026-10-1{n}T05:00:00",
    )
    update_follow_up(item, status="raised")
    set_follow_up_work(item, outcome="prepared" if action_ids else "asked", action_ids=action_ids, night_of=NIGHT)
    return item


def test_normalize_interactive_maps_the_home_tab_ids_and_ignores_the_rest():
    base = {"type": "block_actions", "user": {"id": "UOWNER"}, "channel": {"id": "DPRIV"},
            "message": {"ts": "100.2"}, "action_ts": "101.3", "trigger_id": "T9",
            "container": {"type": "view"}}
    for action_id in digest_actions.ACTION_IDS:
        envelope = _normalize_interactive({**base, "actions": [{"action_id": action_id, "value": "F-000001"}]})
        assert envelope["type"] == "digest" and envelope["action"] == action_id
        assert (envelope["value"], envelope["trigger_id"], envelope["container"]) == ("F-000001", "T9", "view")
    assert _normalize_interactive({**base, "actions": [{"action_id": "fu_done", "value": "F-000001"}]}) is None
    assert _normalize_interactive({"type": "view_submission", "view": {"callback_id": "digest_approve_all"}}) is None


def test_the_listener_applies_update_modal_and_notice():
    from unittest.mock import AsyncMock

    from src.brain.plugins.listeners.slack import _respond_to_digest_result

    web = AsyncMock()
    asyncio.run(_respond_to_digest_result(web, {
        "update": {"channel": "DPRIV", "ts": "1.2", "text": "t", "blocks": []},
        "modal": {"type": "modal"}, "trigger_id": "T1",
        "ephemeral": {"channel": "DPRIV", "text": "n"}, "user": "U1",
    }))
    web.chat_update.assert_awaited_once_with(channel="DPRIV", ts="1.2", text="t", blocks=[])
    web.views_open.assert_awaited_once_with(trigger_id="T1", view={"type": "modal"})
    web.chat_postEphemeral.assert_awaited_once_with(channel="DPRIV", user="U1", text="n")


def test_the_refreshed_home_tab_goes_to_the_configured_owner_only(env, monkeypatch):
    from src.orchestrator import card_actions

    async def changed(msg, identity, *, today):
        return {"updates": [{"channel": "DPRIV", "ts": "1.2", "text": "t", "blocks": []}]}

    monkeypatch.setattr(card_actions, "act", changed)
    msg = {"type": "card", "action": "card_done", "value": "1", "sender": "USOMEONE",
           "group_id": "DPRIV", "target_timestamp": "100.200", "trigger_id": "T1", "container": "message"}
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    assert asyncio.run(digest_actions.handle(msg, OWNER))["home"]["user"] == "UOWNER"
    assert "home" not in asyncio.run(digest_actions.handle(msg, CO_OWNER))
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "")
    assert "home" not in asyncio.run(digest_actions.handle(msg, OWNER))


def home_click(action, value, *, sender=OWNER):
    msg = {"type": "digest", "action": action, "value": value, "sender": "U1",
           "group_id": "", "target_timestamp": "", "trigger_id": "T7", "container": "view"}
    return asyncio.run(digest_actions.handle(msg, sender))


def test_home_tab_details_and_show_open_pop_ups_for_the_owner(env):
    item = worked(1)
    done = worked(2)
    update_follow_up(done, status="done")
    set_follow_up_work(done, outcome="done", action_ids=None, night_of=NIGHT)
    details = home_click("fu_details", item)
    assert details["trigger_id"] == "T7" and "Loop 1" in json.dumps(details["modal"])
    shown = home_click("digest_show_done", "done")
    assert "Loop 2" in json.dumps(shown["modal"])


def test_home_tab_buttons_do_nothing_for_others_or_closed_items(env):
    item = worked(1)
    assert home_click("fu_details", item, sender=CO_OWNER) == {}
    update_follow_up(item, status="done")
    assert home_click("fu_details", item) == {}
    assert home_click("fu_done", item) == {}


def test_the_listener_publishes_home_again_without_a_refused_image():
    from unittest.mock import AsyncMock

    from src.brain.plugins.listeners.slack import _respond_to_digest_result

    web = AsyncMock()
    web.views_publish = AsyncMock(side_effect=[RuntimeError("invalid_blocks"), {"ok": True}])
    view = {"type": "home", "blocks": [{"type": "section"}, {"type": "image", "slack_file": {"id": "F1"}}]}
    asyncio.run(_respond_to_digest_result(web, {"home": {"user": "UOWNER", "view": view}}))
    second = web.views_publish.await_args_list[1].kwargs
    assert second["user_id"] == "UOWNER" and [b["type"] for b in second["view"]["blocks"]] == ["section"]
