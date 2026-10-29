from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from src.brain.media.store import save_png
from src.brain.plugins.base import media_outcome_note
from src.brain.plugins.notifiers.noop import NoopNotifier
from src.brain.plugins.notifiers.slack import SlackNotifier
from src.db import get_db, get_media

REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "health": {"chat": None, "delivery": {"slack": {"channel_id": "DPRIV"}}},
}


@pytest.fixture
def media_env(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", REGISTRY)
    return isolated_db


def _media(place, agent="health"):
    return save_png(b"png-bytes", kind="chart", agent=agent, run_label=f"{agent}@1", area=agent, place=place)


def _client(*, upload=None, post=None):
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value=post or {"ok": True, "ts": "100.200"})
    client.files_upload_v2 = upload or AsyncMock(return_value={"ok": True})
    return client


def _notifier(client):
    notifier = SlackNotifier(bot_token="xoxb-test", channels={"default": {"channel_id": "C_DEFAULT"}})
    notifier._client = client
    return notifier


def _send(notifier, text="hello", **kwargs):
    return asyncio.run(notifier.send(text, **kwargs))


def events(action):
    cursor = get_db().execute("SELECT actor, subject, detail FROM events WHERE action=? ORDER BY id", (action,))
    return cursor.fetchall()


def test_media_upload_follows_the_text_in_the_same_thread(media_env, tmp_path):
    mid = _media("private")
    client = _client()

    result = _send(_notifier(client), area="health", agent="health", media_ids=[mid])

    assert [call[0] for call in client.mock_calls] == ["chat_postMessage", "files_upload_v2"]
    assert result["status"] == "sent" and result["id"] == "100.200"
    upload = client.files_upload_v2.await_args.kwargs
    assert upload["channel"] == client.chat_postMessage.await_args.kwargs["channel"] == "DPRIV"
    assert upload["thread_ts"] == "100.200"
    assert upload["file"] == str(tmp_path / "media" / f"{mid}.png")
    assert (upload["filename"], upload["title"]) == (f"{mid}.png", "Chart")
    assert "refused_media" not in result and "failed_media" not in result
    row = get_media(mid)
    assert (row["sent_channel"], row["sent_ts"]) == ("DPRIV", "100.200")
    [(actor, subject, detail)] = events("media_sent")
    assert (actor, subject) == ("media:health", mid)
    assert json.loads(detail) == {"channel": "DPRIV", "place": "private", "bytes": 9}


def test_the_upload_is_titled_with_the_message_title(media_env):
    mid = _media("private")
    client = _client()
    _send(_notifier(client), title="Weekly chart", area="health", media_ids=[mid])
    assert client.files_upload_v2.await_args.kwargs["title"] == "Weekly chart"


def test_an_untitled_drawing_is_titled_picture_not_its_id(media_env):
    mid = save_png(b"png-bytes", kind="image", agent="health", run_label="health@1", area="health", place="private")
    client = _client()
    _send(_notifier(client), area="health", media_ids=[mid])
    assert client.files_upload_v2.await_args.kwargs["title"] == "Picture"


def test_a_threaded_message_gets_its_media_in_the_thread_it_was_posted_to(media_env):
    mid = _media("private")
    client = _client(post={"ok": True, "ts": "300.400"})
    _send(_notifier(client), area="health", quote_timestamp="50.60", media_ids=[mid])
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "50.60"
    assert client.files_upload_v2.await_args.kwargs["thread_ts"] == "50.60"
    assert get_media(mid)["sent_ts"] == "300.400"


def test_media_from_the_other_place_is_refused_and_named(media_env):
    shared = _media("shared", agent="family")
    client = _client()

    result = _send(_notifier(client), area="health", media_ids=[shared])

    assert result["status"] == "sent" and result["refused_media"] == [shared]
    client.files_upload_v2.assert_not_awaited()
    assert get_media(shared)["sent_ts"] is None


def test_shared_media_go_to_the_shared_channel_and_private_media_do_not(media_env):
    shared, private = _media("shared", agent="family"), _media("private")
    client = _client()

    result = _send(_notifier(client), area="family", agent="alfred", media_ids=[private, shared])

    assert result["refused_media"] == [private]
    upload = client.files_upload_v2.await_args.kwargs
    assert (upload["channel"], upload["filename"]) == ("CSHARED", f"{shared}.png")


def test_unknown_expired_and_missing_media_are_refused(media_env, tmp_path):
    gone = _media("private")
    (tmp_path / "media" / f"{gone}.png").unlink()
    ok = _media("private")
    client = _client()

    result = _send(_notifier(client), area="health", media_ids=["m-000000000000", gone, ok])

    assert result["refused_media"] == ["m-000000000000", gone]
    assert [c.kwargs["filename"] for c in client.files_upload_v2.await_args_list] == [f"{ok}.png"]


def test_media_are_uploaded_in_order_and_a_repeated_id_once(media_env):
    first, second = _media("private"), _media("private")
    client = _client()
    _send(_notifier(client), area="health", media_ids=[second, first, second])
    assert [c.kwargs["filename"] for c in client.files_upload_v2.await_args_list] == [f"{second}.png", f"{first}.png"]


def test_the_destination_channels_own_place_decides(media_env):
    private, shared = _media("private"), _media("shared", agent="family")
    client = _client()
    notifier = _notifier(client)

    to_default = _send(notifier, area="not-an-area", place="private", media_ids=[private])
    to_shared = _send(notifier, area="family", place="private", media_ids=[private])
    to_private = _send(notifier, area="health", place="shared", media_ids=[shared])
    unknown_area = _send(notifier, area="not-an-area", media_ids=[private])
    right = _send(notifier, area="health", place="private", media_ids=[private])

    for wrong in (to_default, to_shared, to_private, unknown_area):
        assert wrong["status"] == "sent" and wrong["refused_media"] in ([private], [shared])
    assert "refused_media" not in right
    assert [c.kwargs["channel"] for c in client.files_upload_v2.await_args_list] == ["DPRIV"]


def test_media_made_for_the_destination_are_refused_to_a_caller_of_the_other_place(media_env):
    shared = _media("shared", agent="family")
    client = _client()

    result = _send(_notifier(client), area="family", place="private", media_ids=[shared])

    assert result["status"] == "sent" and result["refused_media"] == [shared]
    client.files_upload_v2.assert_not_awaited()
    assert get_media(shared)["sent_ts"] is None


def test_the_integrations_channel_map_gives_a_channel_its_place(media_env):
    shared, private = _media("shared", agent="family"), _media("private")
    client = _client()
    notifier = SlackNotifier(bot_token="xoxb-test", channels={
        "alfred": {"channel_id": "CNEWSHARED"}, "family": {"channel_id": "CNEWSHARED"},
    })
    notifier._client = client

    accepted = _send(notifier, area="family", media_ids=[shared])
    refused = _send(notifier, area="family", place="private", media_ids=[private])

    assert "refused_media" not in accepted and refused["refused_media"] == [private]
    upload = client.files_upload_v2.await_args.kwargs
    assert (upload["channel"], upload["filename"]) == ("CNEWSHARED", f"{shared}.png")
    assert client.files_upload_v2.await_count == 1


def test_more_than_four_media_ids_are_refused_before_posting(media_env):
    ids = [_media("private") for _ in range(5)]
    client = _client()

    result = _send(_notifier(client), area="health", media_ids=ids)

    assert result["status"] == "failed" and "at most 4 media ids" in result["error"]
    client.chat_postMessage.assert_not_awaited()
    client.files_upload_v2.assert_not_awaited()


def test_a_failed_upload_is_reported_and_the_text_stays(media_env):
    failing, fine = _media("private"), _media("private")
    error = SlackApiError("upload failed", {"ok": False, "error": "missing_scope"})
    upload = AsyncMock(side_effect=[error, {"ok": True}])
    client = _client(upload=upload)

    result = _send(_notifier(client), area="health", agent="health", media_ids=[failing, fine])

    assert result["status"] == "sent" and result["id"] == "100.200"
    assert result["failed_media"] == [failing] and result["media_errors"] == {failing: "missing_scope"}
    assert get_media(failing)["sent_ts"] is None and get_media(fine)["sent_ts"] == "100.200"
    assert [subject for _, subject, _ in events("media_sent")] == [fine]


def test_a_response_that_says_not_ok_counts_as_a_failed_upload(media_env):
    mid = _media("private")
    client = _client(upload=AsyncMock(return_value={"ok": False, "error": "file_uploads_disabled"}))

    result = _send(_notifier(client), area="health", media_ids=[mid])

    assert result["failed_media"] == [mid] and result["media_errors"] == {mid: "file_uploads_disabled"}


def test_a_failure_that_is_not_a_slack_error_is_reported_with_its_text_cut_short(media_env):
    mid = _media("private")
    client = _client(upload=AsyncMock(side_effect=TimeoutError("x" * 500)))

    result = _send(_notifier(client), area="health", media_ids=[mid])

    assert result["failed_media"] == [mid] and len(result["media_errors"][mid]) <= 100


def test_nothing_is_uploaded_when_the_text_was_not_posted(media_env):
    mid = _media("private")
    client = _client(post={"ok": False, "error": "channel_not_found"})

    result = _send(_notifier(client), area="health", media_ids=[mid])

    assert result["status"] == "failed"
    client.files_upload_v2.assert_not_awaited()
    assert "refused_media" not in result


def test_a_message_without_media_is_unchanged(media_env):
    client = _client()
    result = _send(_notifier(client), area="health")
    client.files_upload_v2.assert_not_awaited()
    assert not {"refused_media", "failed_media", "media_errors"} & result.keys()


def test_the_noop_notifier_accepts_media_and_posts_nothing():
    result = asyncio.run(NoopNotifier().send("hi", media_ids=["m-0123456789ab"], place="private"))
    assert result == {"status": "noop", "id": None}


@pytest.mark.parametrize("result, note", [
    ({}, ""),
    ({"refused_media": []}, ""),
    ({"refused_media": ["m-a", "m-b"]}, "refused media: m-a, m-b (no such media, deleted, or made for another place)"),
    ({"failed_media": ["m-a"], "media_errors": {"m-a": "missing_scope"}}, "failed media: m-a (missing_scope)"),
    ({"failed_media": ["m-a"]}, "failed media: m-a"),
    (
        {"refused_media": ["m-a"], "failed_media": ["m-b"], "media_errors": {"m-b": "x"}},
        "refused media: m-a (no such media, deleted, or made for another place)\nfailed media: m-b (x)",
    ),
])
def test_the_note_a_run_sees_names_each_media_that_was_not_posted(result, note):
    assert media_outcome_note(result) == note
