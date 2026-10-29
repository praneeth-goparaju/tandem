from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import src.brain.plugins.listeners.slack as listener
from src.brain.media.voice import ClipOverLimit
from src.brain.plugins.listeners.slack import (
    _chat_voice_over_limit,
    _download_chat_voice,
    _normalize_event,
)
from src.orchestrator import reply_routing

LIMIT = 25 * 1024 * 1024
CLIP_URL = "https://files.slack.com/files-pri/T1-F1/voice.webm"


def _run(coro):
    return asyncio.run(coro)


def _file(**fields) -> dict:
    return {"url_private": CLIP_URL, **fields}


@pytest.mark.parametrize("files", [
    [],
    [_file(subtype="slack_audio", size=LIMIT, duration_ms=600_000)],
    [_file(subtype="slack_audio")],
    [_file(subtype="slack_audio", size=-1)],
    [{"mimetype": "image/png", "size": LIMIT + 1, "url_private": CLIP_URL}],
    [{"mimetype": "application/zip", "size": LIMIT + 1, "url_private": CLIP_URL}],
    [{"subtype": "slack_audio", "size": LIMIT + 1, "url_private": "https://evil.test/clip"}],
    ["x", None, 3],
])
def test_over_limit_is_false_for_everything_else(files):
    assert _chat_voice_over_limit(files) is False


@pytest.fixture
def people(monkeypatch):
    monkeypatch.delenv("BRAIN_MEDIA", raising=False)
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {
        "authorized_senders": {"UCOOWNER": {"name": "Co-owner", "areas": ["family"]}},
    })


def _envelope(sender: str, **changes) -> dict:
    event = {
        "user": sender, "text": "", "channel": "C1", "channel_type": "channel", "ts": "500.1",
        "files": [_file(subtype="slack_audio", mimetype="audio/webm", size=4000, duration_ms=8000, user=sender)],
        **changes,
    }
    return _run(_normalize_event(event, "message", web_client=SimpleNamespace(token="xoxb-secret")))


@pytest.mark.parametrize("sender", ["UOWNER", "UCOOWNER"])
def test_the_owner_and_an_authorized_sender_get_a_lazy_voice_loader(people, monkeypatch, sender):
    calls = []

    async def fake_download(candidates, web_client):
        calls.append((candidates, web_client.token))
        return b"RIFF\x00\x00\x00\x00WAVE"

    monkeypatch.setattr(listener, "_download_chat_voice", fake_download)

    env = _envelope(sender)

    assert callable(env["_load_voice"]) and calls == []
    assert _run(env["_load_voice"]()) == b"RIFF\x00\x00\x00\x00WAVE"
    assert calls == [([(CLIP_URL, "audio/webm")], "xoxb-secret")]
    assert env["text"] == "" and env["image_count"] == 0 and env["_load_images"] is None


@pytest.mark.parametrize("sender", ["USTRANGER", ""])
def test_anyone_else_gets_no_voice_loader(people, sender):
    assert _envelope(sender)["_load_voice"] is None


@pytest.mark.parametrize("files", [
    [],
    [{"mimetype": "image/png", "size": 10, "url_private": "https://files.slack.com/p", "user": "UOWNER"}],
    [{"subtype": "slack_audio", "url_private": "https://evil.test/clip", "user": "UOWNER"}],
    [{"subtype": "slack_audio", "url_private": "http://files.slack.com/clip", "user": "UOWNER"}],
    [{"subtype": "slack_audio", "size": LIMIT + 1, "url_private": "https://evil.test/clip", "user": "UOWNER"}],
])
def test_no_loader_without_a_clip_to_fetch_or_to_refuse(people, files):
    assert _envelope("UOWNER", files=files)["_load_voice"] is None


@pytest.mark.parametrize("fields", [
    {}, {"subtype": "other"}, {"mimetype": "text/plain"}, {"mimetype": "application/zip"},
    {"mimetype": "application/pdf"},
])
def test_a_file_that_is_not_a_clip_gets_no_loader(people, fields):
    assert _envelope("UOWNER", files=[_file(user="UOWNER", **fields)])["_load_voice"] is None


def _fetched_candidates(monkeypatch, files):
    calls = []

    async def fake_download(candidates, web_client):
        calls.append(candidates)
        return b"OggS"

    monkeypatch.setattr(listener, "_download_chat_voice", fake_download)
    loader = _envelope("UOWNER", files=files)["_load_voice"]
    assert callable(loader)
    _run(loader())
    return calls


@pytest.mark.parametrize("fields", [{}, {"size": LIMIT}, {"duration_ms": 600_000}])
def test_a_clip_declaring_no_more_than_the_limits_is_fetched(people, monkeypatch, fields):
    files = [_file(subtype="slack_audio", user="UOWNER", **fields)]
    assert _fetched_candidates(monkeypatch, files) == [[(CLIP_URL, "")]]


def test_a_clip_declaring_a_negative_size_gets_no_loader(people):
    files = [_file(subtype="slack_audio", user="UOWNER", size=-1)]
    assert _envelope("UOWNER", files=files)["_load_voice"] is None


def test_the_download_url_is_fetched_in_preference_to_the_view_url(people, monkeypatch):
    files = [{
        "subtype": "slack_audio", "user": "UOWNER",
        "url_private": "https://files.slack.com/view/a", "url_private_download": "https://files.slack.com/download/a",
    }]
    assert _fetched_candidates(monkeypatch, files) == [[("https://files.slack.com/download/a", "")]]


def test_the_declared_type_is_passed_on_lower_cased(people, monkeypatch):
    files = [_file(user="UOWNER", mimetype="AUDIO/MPEG")]
    assert _fetched_candidates(monkeypatch, files) == [[(CLIP_URL, "audio/mpeg")]]


def test_entries_that_are_not_file_objects_are_skipped(people, monkeypatch):
    files = ["x", None, 3, [], _file(subtype="slack_audio", user="UOWNER")]
    assert _fetched_candidates(monkeypatch, files) == [[(CLIP_URL, "")]]


@pytest.mark.parametrize("fields", [{"size": LIMIT + 1}, {"duration_ms": 600_001}])
@pytest.mark.parametrize("sender", ["UOWNER", "UCOOWNER"])
def test_a_clip_over_the_limits_gets_a_loader_that_refuses_it(people, sender, fields):
    env = _envelope(sender, files=[_file(subtype="slack_audio", user=sender, **fields)])

    assert callable(env["_load_voice"])
    with pytest.raises(ClipOverLimit):
        _run(env["_load_voice"]())


@pytest.mark.parametrize("fields", [
    {"subtype": "slack_video", "duration_ms": 1_800_000},
    {"mimetype": "audio/m4a", "size": LIMIT + 1},
])
def test_every_kind_of_clip_over_the_limits_gets_a_loader_that_refuses_it(people, fields):
    env = _envelope("UOWNER", files=[_file(user="UOWNER", **fields)])

    with pytest.raises(ClipOverLimit):
        _run(env["_load_voice"]())


def test_a_clip_over_the_limits_beside_a_picture_still_gets_a_loader_that_refuses_it(people):
    picture = {"mimetype": "image/png", "size": 10, "url_private": "https://files.slack.com/p", "user": "UOWNER"}
    env = _envelope("UOWNER", files=[picture, _file(subtype="slack_audio", size=LIMIT + 1, user="UOWNER")])

    with pytest.raises(ClipOverLimit):
        _run(env["_load_voice"]())


def test_a_stranger_gets_no_loader_for_a_clip_over_the_limits(people):
    env = _envelope("USTRANGER", files=[_file(subtype="slack_audio", size=LIMIT + 1, user="USTRANGER")])
    assert env["_load_voice"] is None


@pytest.mark.parametrize("uploader", ["UCOOWNER", "", None])
def test_a_clip_the_sender_did_not_upload_gets_no_loader(people, uploader):
    fields = {} if uploader is None else {"user": uploader}
    for files in (
        [_file(subtype="slack_audio", **fields)],
        [_file(mimetype="audio/m4a", **fields)],
        [_file(subtype="slack_audio", size=LIMIT + 1, **fields)],
    ):
        assert _envelope("UOWNER", files=files)["_load_voice"] is None


def test_the_senders_own_clip_beside_someone_elses_is_the_one_fetched(people, monkeypatch):
    calls = []

    async def fake_download(candidates, web_client):
        calls.append(candidates)
        return b"OggS"

    monkeypatch.setattr(listener, "_download_chat_voice", fake_download)
    files = [
        {"subtype": "slack_audio", "url_private": "https://files.slack.com/theirs", "user": "UCOOWNER"},
        {"subtype": "slack_audio", "url_private": "https://files.slack.com/mine", "user": "UOWNER"},
    ]

    _run(_envelope("UOWNER", files=files)["_load_voice"]())

    assert calls == [[("https://files.slack.com/mine", "")]]


@pytest.mark.parametrize("fields, attached", [
    ({"subtype": "slack_audio", "mimetype": "audio/webm"}, False),
    ({"subtype": "slack_video", "mimetype": "video/mp4"}, False),
    ({"mimetype": "audio/m4a"}, True),
    ({"mimetype": "audio/mpeg", "subtype": "other"}, True),
])
def test_an_audio_file_the_sender_attached_is_marked_as_attached(people, fields, attached):
    env = _envelope("UOWNER", files=[_file(user="UOWNER", **fields)])
    assert callable(env["_load_voice"]) and env["voice_attached"] is attached


def test_a_message_without_a_clip_is_not_marked_as_attached_audio(people):
    assert _envelope("UOWNER", files=[])["voice_attached"] is False


def test_a_usable_clip_beside_one_over_the_limits_is_the_one_downloaded(people, monkeypatch):
    calls = []

    async def fake_download(candidates, web_client):
        calls.append(candidates)
        return b"RIFF\x00\x00\x00\x00WAVE"

    monkeypatch.setattr(listener, "_download_chat_voice", fake_download)
    files = [
        _file(subtype="slack_audio", size=LIMIT + 1, user="UOWNER"),
        {"subtype": "slack_audio", "url_private": "https://files.slack.com/second", "user": "UOWNER"},
    ]

    env = _envelope("UOWNER", files=files)

    assert _run(env["_load_voice"]()) == b"RIFF\x00\x00\x00\x00WAVE"
    assert calls == [[("https://files.slack.com/second", "")]]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_with_the_media_switch_off_no_clip_gets_a_loader(people, monkeypatch, value):
    monkeypatch.setenv("BRAIN_MEDIA", value)

    assert _envelope("UOWNER")["_load_voice"] is None
    assert _envelope(
        "UOWNER", files=[_file(subtype="slack_audio", size=LIMIT + 1, user="UOWNER")],
    )["_load_voice"] is None


@pytest.mark.parametrize("value", [None, "", "1", "true", "yes", "on"])
def test_with_the_media_switch_on_or_unset_a_clip_gets_a_loader(people, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("BRAIN_MEDIA", value)

    assert callable(_envelope("UOWNER")["_load_voice"])


def test_a_message_without_files_has_no_voice_loader(people):
    event = {"user": "UOWNER", "text": "hello", "channel": "C1", "channel_type": "channel", "ts": "500.2"}
    env = _run(_normalize_event(event, "message", web_client=None))
    assert env["_load_voice"] is None and env["text"] == "hello"


def test_a_screen_recording_attached_to_a_question_gets_no_voice_loader(people):
    recording = {"mimetype": "video/quicktime", "size": 90 * 1024 * 1024, "url_private": CLIP_URL, "user": "UOWNER"}
    env = _envelope("UOWNER", text="why does this fail?", files=[recording])
    assert env["_load_voice"] is None and env["text"] == "why does this fail?"


def test_a_failing_identity_lookup_costs_the_voice_loader_and_nothing_else(people, monkeypatch):
    def unreadable(_sender):
        raise RuntimeError("integrations file unreadable")

    monkeypatch.setattr(reply_routing, "sender_identity", unreadable)

    env = _envelope("UOWNER", text="hello")

    assert env["_load_voice"] is None
    assert env["type"] == "reply" and env["text"] == "hello"


class _Response:
    def __init__(self, body: bytes, status: int):
        self.status = status
        self.content = asyncio.StreamReader()
        self.content.feed_data(body)
        self.content.feed_eof()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _serve(monkeypatch, *replies):
    seen: dict = {"urls": []}
    queue = list(replies)

    class Session:
        def __init__(self, **kwargs):
            seen["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            seen["urls"].append(url)
            seen["get"] = kwargs
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            body, status = reply if isinstance(reply, tuple) else (reply, 200)
            return _Response(body, status)

    monkeypatch.setattr(listener.aiohttp, "ClientSession", Session)
    return seen


def test_the_download_uses_bearer_auth_without_redirects_and_returns_the_bytes(monkeypatch):
    seen = _serve(monkeypatch, b"OggS-clip")

    out = _run(_download_chat_voice([(CLIP_URL, "audio/ogg")], SimpleNamespace(token="xoxb-secret")))

    assert out == b"OggS-clip"
    assert seen["session"]["headers"] == {"Authorization": "Bearer xoxb-secret"}
    assert seen["session"]["trust_env"] is False and seen["get"]["allow_redirects"] is False
    assert seen["urls"] == [CLIP_URL]


def test_the_download_takes_the_first_candidate_only(monkeypatch):
    seen = _serve(monkeypatch, b"first", b"second")

    out = _run(_download_chat_voice(
        [("https://files.slack.com/one", "audio/ogg"), ("https://files.slack.com/two", "audio/ogg")],
        SimpleNamespace(token="xoxb-secret"),
    ))

    assert out == b"first" and seen["urls"] == ["https://files.slack.com/one"]


def test_the_download_enforces_the_size_limit_on_the_bytes(monkeypatch):
    monkeypatch.setattr(listener, "_MAX_CHAT_VOICE_BYTES", 4)
    _serve(monkeypatch, b"1234", b"12345", b"1" * 4000)
    client = SimpleNamespace(token="xoxb-secret")

    assert _run(_download_chat_voice([(CLIP_URL, "")], client)) == b"1234"
    for _ in range(2):
        with pytest.raises(ClipOverLimit):
            _run(_download_chat_voice([(CLIP_URL, "")], client))


@pytest.mark.parametrize("failure", [(b"", 404), (b"x", 302), TimeoutError("slow")])
def test_a_failed_download_returns_none(monkeypatch, failure):
    _serve(monkeypatch, failure)
    assert _run(_download_chat_voice([(CLIP_URL, "")], SimpleNamespace(token="xoxb-secret"))) is None


@pytest.mark.parametrize("candidates, client", [
    ([], SimpleNamespace(token="xoxb-secret")),
    ([(CLIP_URL, "")], SimpleNamespace(token="")),
    ([(CLIP_URL, "")], SimpleNamespace()),
])
def test_nothing_is_fetched_without_a_candidate_or_a_token(monkeypatch, candidates, client):
    seen = _serve(monkeypatch)
    assert _run(_download_chat_voice(candidates, client)) is None
    assert seen["urls"] == [] and "session" not in seen
