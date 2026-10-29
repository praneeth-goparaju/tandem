from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.brain.plugins.listeners.slack import (
    _chat_image_candidates,
    _download_chat_images,
    _normalize_event,
    _normalize_interactive,
    _respond_to_approval_result,
    _update_approval_message,
)
from src.brain.plugins.notifiers.slack import APPROVE_ACTION_ID, FINDING_EVENT_TYPE


def _run(coro):
    return asyncio.run(coro)


def test_approval_button_normalizes_to_approval_envelope():
    payload = {
        "type": "block_actions",
        "user": {"id": "UOWNER"},
        "channel": {"id": "CFAMILY"},
        "message": {"ts": "100.2"},
        "action_ts": "101.3",
        "actions": [{"action_id": APPROVE_ACTION_ID, "value": "abc123def456"}],
    }
    assert _normalize_interactive(payload) == {
        "type": "approval",
        "decision": "approved",
        "action_id": "abc123def456",
        "sender": "UOWNER",
        "group_id": "CFAMILY",
        "timestamp": "101.3",
        "target_timestamp": "100.2",
    }


def test_interactive_rejects_unknown_or_malformed_action():
    base = {
        "type": "block_actions", "user": {"id": "U"},
        "channel": {"id": "C"}, "message": {"ts": "1"},
    }
    assert _normalize_interactive({**base, "actions": [{"action_id": "other", "value": "abc123def456"}]}) is None
    assert _normalize_interactive({**base, "actions": [{"action_id": APPROVE_ACTION_ID, "value": "bad"}]}) is None


def test_approved_button_says_execution_is_only_queued():
    action_id = "abc123def456"
    web = AsyncMock()
    payload = {
        "channel": {"id": "CFAMILY"},
        "message": {
            "ts": "100.2",
            "text": "Approval",
            "blocks": [
                {"type": "section", "text": {"type": "plain_text", "text": "Header"}},
                {"type": "actions", "block_id": f"brain_approval_{action_id}"},
                {"type": "section", "text": {"type": "plain_text", "text": "Payload"}},
            ],
        },
    }

    _run(_update_approval_message(
        web, payload, {"outcome": "approved", "action_id": action_id},
    ))

    blocks = web.chat_update.await_args.kwargs["blocks"]
    assert blocks[1]["elements"][0]["text"] == (
        f"*Approved — queued for execution* · `{action_id}`"
    )
    assert blocks[0]["text"]["text"] == "Header"
    assert blocks[2]["text"]["text"] == "Payload"


def test_denied_approval_gets_private_feedback_without_rewriting_card():
    web = AsyncMock()
    payload = {
        "channel": {"id": "CFAMILY"},
        "user": {"id": "UCLICKER"},
        "message": {"ts": "100.2", "text": "Approval"},
    }

    _run(_respond_to_approval_result(
        web,
        payload,
        {"outcome": "unauthorized", "action_id": "abc123def456"},
    ))

    web.chat_update.assert_not_awaited()
    web.chat_postEphemeral.assert_awaited_once_with(
        channel="CFAMILY",
        user="UCLICKER",
        text=(
            "Approval was not applied: this channel is not mapped for the "
            "action, or you are not authorized. No action ran."
        ),
        thread_ts="100.2",
    )


def test_reaction_added_normalizes_to_reaction_envelope():
    event = {
        "user": "UABC",
        "reaction": "thumbsup",
        "item": {"channel": "CHEALTH", "ts": "100.1"},
        "event_ts": "200.2",
    }
    env = _run(_normalize_event(event, "reaction_added", web_client=None))
    assert env == {
        "type": "reaction",
        "emoji": "thumbsup",
        "sender": "UABC",
        "group_id": "CHEALTH",
        "timestamp": "200.2",
        "target_timestamp": "100.1",
        "quoted_timestamp": None,
        "text": "",
        "quoted_text": "",
        "finding_id": None,
        "removed": False,
    }


def test_reaction_removed_sets_removed_flag():
    event = {
        "user": "UABC",
        "reaction": "thumbsup",
        "item": {"channel": "C", "ts": "1.1"},
        "event_ts": "2.2",
    }
    env = _run(_normalize_event(event, "reaction_removed", web_client=None))
    assert env["type"] == "reaction"
    assert env["removed"] is True


@pytest.mark.parametrize("subtype", ["message_changed", "message_deleted", "bot_message", "channel_join"])
def test_message_subtypes_are_filtered_out(subtype):
    event = {"text": "hi", "channel": "C", "ts": "1.1", "subtype": subtype}
    assert _run(_normalize_event(event, "message", web_client=None)) is None


def test_bot_messages_are_filtered_out():
    event = {"text": "hi", "channel": "C", "ts": "1.1", "bot_id": "B123"}
    assert _run(_normalize_event(event, "message", web_client=None)) is None


def test_unknown_event_type_returns_none():
    assert _run(_normalize_event({}, "channel_created", web_client=None)) is None


def test_top_level_message_has_no_quoted_or_finding():
    event = {
        "user": "UEXAMPLEOWNER",
        "text": "hello bot",
        "channel": "DDM12345",
        "channel_type": "im",
        "ts": "300.3",
    }
    env = _run(_normalize_event(event, "message", web_client=None))
    assert env["type"] == "reply"
    assert env["text"] == "hello bot"
    assert env["sender"] == "UEXAMPLEOWNER"
    assert env["group_id"] == "DDM12345"
    assert env["channel_type"] == "im"
    assert env["timestamp"] == "300.3"
    assert env["quoted_timestamp"] is None
    assert env["finding_id"] is None
    assert env["quoted_text"] == ""
    assert env["quoted_from_bot"] is False
    assert env["images"] == []
    assert env["image_count"] == 0
    assert env["_load_images"] is None


def test_message_images_are_loaded_lazily(monkeypatch):
    import src.brain.plugins.listeners.slack as module

    expected = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"}]
    calls = 0

    async def fake_download(candidates, web_client):
        nonlocal calls
        calls += 1
        assert candidates == [("https://files.slack.com/photo.jpg", "image/jpeg")]
        return expected

    monkeypatch.setattr(module, "_download_chat_images", fake_download)
    event = {
        "user": "UOWNER", "text": "what is this?", "channel": "C1",
        "channel_type": "channel", "ts": "301.1",
        "files": [{
            "name": "photo.jpg", "mimetype": "image/jpeg", "size": 100,
            "url_private": "https://files.slack.com/photo.jpg",
        }],
    }
    env = _run(_normalize_event(event, "message", web_client=object()))
    assert env["images"] == []
    assert calls == 0
    assert _run(env["_load_images"]()) == expected
    assert calls == 1


def test_image_candidates_reject_non_slack_urls_and_bad_ports():
    candidates, image_count = _chat_image_candidates([
        {"mimetype": "image/png", "url_private": "https://evil.test/x"},
        {"mimetype": "image/png", "url_private": "https://files.slack.com:bad/x"},
        {"mimetype": "image/png", "url_private": "https://files.slack.com/x"},
    ])
    assert image_count == 3
    assert candidates == [("https://files.slack.com/x", "image/png")]


def test_image_candidates_enforce_per_file_and_aggregate_size_limits():
    mib = 1024 * 1024
    candidates, image_count = _chat_image_candidates([
        {"mimetype": "image/png", "size": 10 * mib, "url_private": "https://files.slack.com/1"},
        {"mimetype": "image/png", "size": 10 * mib, "url_private": "https://files.slack.com/2"},
        {"mimetype": "image/png", "size": 1, "url_private": "https://files.slack.com/3"},
    ])
    assert image_count == 3
    assert [url for url, _mime in candidates] == [
        "https://files.slack.com/1", "https://files.slack.com/2",
    ]


def test_image_download_uses_bearer_auth_without_redirects(monkeypatch):
    import src.brain.plugins.listeners.slack as module

    seen = {}

    class Response:
        status = 200

        def __init__(self):
            self.content = asyncio.StreamReader()
            self.content.feed_data(b"image")
            self.content.feed_eof()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    out = _run(_download_chat_images(
        [("https://files.slack.com/image", "image/png")],
        SimpleNamespace(token="xoxb-secret"),
    ))
    assert seen["headers"] == {"Authorization": "Bearer xoxb-secret"}
    assert seen["trust_env"] is False and seen["allow_redirects"] is False
    assert out == [{"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}]


def test_image_download_enforces_actual_aggregate_bytes(monkeypatch):
    import src.brain.plugins.listeners.slack as module

    monkeypatch.setattr(module, "_MAX_CHAT_IMAGE_BYTES", 4)
    monkeypatch.setattr(module, "_MAX_CHAT_IMAGE_TOTAL_BYTES", 5)
    bodies = [b"1234", b"56"]

    class Response:
        status = 200

        def __init__(self, body):
            self.content = asyncio.StreamReader()
            self.content.feed_data(body)
            self.content.feed_eof()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return Response(bodies.pop(0))

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    out = _run(_download_chat_images(
        [
            ("https://files.slack.com/one", "image/png"),
            ("https://files.slack.com/two", "image/png"),
        ],
        SimpleNamespace(token="xoxb-secret"),
    ))

    assert out == [{"type": "image", "data": "MTIzNA==", "mimeType": "image/png"}]


def _threaded_event() -> dict:
    return {
        "user": "UEXAMPLEOWNER",
        "text": "yes do it",
        "channel": "CHEALTH",
        "channel_type": "channel",
        "ts": "400.4",
        "thread_ts": "350.3",
    }


def test_threaded_message_picks_up_finding_id_from_metadata():
    parent_msg = {
        "text": "Pollen high today",
        "metadata": {
            "event_type": FINDING_EVENT_TYPE,
            "event_payload": {"finding_id": "F99", "agent": "health", "area": "health"},
        },
    }
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [parent_msg]})

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env["finding_id"] == "F99"
    assert env["parent_agent"] == "health"
    assert env["parent_area"] == "health"
    assert env["quoted_text"] == "Pollen high today"
    assert env["quoted_timestamp"] == "350.3"
    web.conversations_replies.assert_awaited_once()


def test_threaded_message_ignores_sender_controlled_ref_in_current_body():
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [{"text": ""}]})
    event = _threaded_event()
    event["text"] = "yes do it ref:OLD123"

    env = _run(_normalize_event(event, "message", web_client=web))

    assert env["finding_id"] is None
    assert env["parent_agent"] is None
    assert env["parent_area"] is None


def test_threaded_message_falls_back_to_ref_regex_in_quoted_text():
    parent_msg = {"text": "Heads up — ref:LEGACY42 in the parent body"}
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [parent_msg]})

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env["finding_id"] == "LEGACY42"


def test_threaded_message_with_no_finding_or_ref():
    parent_msg = {"text": "just a chat thread, no finding"}
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [parent_msg]})

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env["finding_id"] is None
    assert env["quoted_text"] == "just a chat thread, no finding"


def test_threaded_message_swallows_conversations_replies_failure():
    web = AsyncMock()
    web.conversations_replies = AsyncMock(side_effect=RuntimeError("slack down"))

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env is not None
    assert env["finding_id"] is None
    assert env["quoted_text"] == ""
    assert env["quoted_from_bot"] is False


@pytest.mark.parametrize(
    ("parent", "from_bot"),
    [
        ({"text": "F-000001 Did the sync get fixed?", "bot_id": "B0ALFRED"}, True),
        ({"text": "F-000001 Did the sync get fixed?", "subtype": "bot_message"}, True),
        ({"text": "F-000001 Did the sync get fixed?", "user": "UEXAMPLEOWNER"}, False),
        ({"text": "F-000001 Did the sync get fixed?", "bot_id": ""}, False),
        ({"text": "F-000001 Did the sync get fixed?"}, False),
    ],
    ids=["bot_id", "bot_message subtype", "typed by a person", "empty bot_id", "no author"],
)
def test_threaded_message_says_whether_its_parent_was_posted_by_a_bot(parent, from_bot):
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [parent]})

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env["quoted_from_bot"] is from_bot
    assert env["quoted_text"] == "F-000001 Did the sync get fixed?"


def test_thread_root_message_does_not_fetch_replies():
    web = AsyncMock()
    web.conversations_replies = AsyncMock()

    event = _threaded_event()
    event["thread_ts"] = event["ts"]

    env = _run(_normalize_event(event, "message", web_client=web))

    web.conversations_replies.assert_not_called()
    assert env["finding_id"] is None


def test_metadata_with_wrong_event_type_is_ignored():
    parent_msg = {
        "text": "parent",
        "metadata": {
            "event_type": "some_other_app_event",
            "event_payload": {"finding_id": "SHOULD_NOT_USE"},
        },
    }
    web = AsyncMock()
    web.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [parent_msg]})

    env = _run(_normalize_event(_threaded_event(), "message", web_client=web))

    assert env["finding_id"] is None
    assert env["parent_agent"] is None
