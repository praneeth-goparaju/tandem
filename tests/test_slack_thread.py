from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.brain import slack_thread
from src.brain.slack_thread import fetch_thread, format_thread_for_prompt


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(slack_thread, "_CACHE", {})


def _fake_client(messages):
    c = AsyncMock()
    c.conversations_replies = AsyncMock(
        return_value={"ok": True, "messages": messages}
    )
    return c


def test_fetch_thread_returns_messages_and_caches() -> None:
    msgs = [
        {"user": "U1", "text": "root", "ts": "1.0"},
        {"user": "U2", "text": "reply", "ts": "1.1", "thread_ts": "1.0"},
    ]
    client = _fake_client(msgs)

    out1 = asyncio.run(fetch_thread(client, "C1", "1.0"))
    out2 = asyncio.run(fetch_thread(client, "C1", "1.0"))

    assert out1 == msgs
    assert out2 == msgs
    client.conversations_replies.assert_awaited_once()


def test_fetch_thread_bypasses_cache_after_ttl() -> None:
    client = _fake_client([{"user": "U", "text": "x", "ts": "2.0"}])

    asyncio.run(fetch_thread(client, "C", "2.0", ttl=0))
    asyncio.run(fetch_thread(client, "C", "2.0", ttl=0))

    assert client.conversations_replies.await_count == 2


def test_fetch_thread_returns_empty_on_api_error() -> None:
    client = AsyncMock()
    client.conversations_replies = AsyncMock(
        return_value={"ok": False, "error": "channel_not_found"}
    )

    out = asyncio.run(fetch_thread(client, "C", "1.0"))
    assert out == []


def test_format_thread_for_prompt_uses_user_names() -> None:
    msgs = [
        {"user": "U1", "text": "root message", "ts": "1.0"},
        {"user": "U2", "text": "a reply", "ts": "1.1"},
        {"bot_id": "B1", "text": "bot reply", "ts": "1.2"},
    ]
    out = format_thread_for_prompt(msgs, user_names={"U1": "Alice", "U2": "Bob"})
    assert "[Alice]: root message" in out
    assert "[Bob]: a reply" in out
    assert "[brain]: bot reply" in out


def test_format_thread_skips_empty_and_joins() -> None:
    msgs = [
        {"user": "U", "text": "", "ts": "1.0"},
        {"user": "U", "text": "something", "ts": "1.1"},
        {"subtype": "channel_join", "user": "U", "text": "joined", "ts": "1.2"},
    ]
    out = format_thread_for_prompt(msgs, user_names={"U": "P"})
    assert out == "[P]: something"


def test_format_thread_bounds_aggregate_prompt_text() -> None:
    out = format_thread_for_prompt([{"user": "U", "text": "x" * 20_000}])
    assert out.startswith("[earlier thread text truncated]")
    assert len(out) < 12_100
