from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _patches(parent, notifier_mock=None):
    return (
        patch("src.brain.mcp.tools._icloud.require_creds", return_value=("u@example.com", "pw")),
        patch("src.brain.mcp.tools._mail_quote.fetch_parent_for_quote", return_value=parent),
        patch("src.brain.mcp._common.notifier",
              return_value=notifier_mock or MagicMock()),
        patch("src.brain.notifications_audit.record_notification"),
    )


def _enter(*ctxs):
    return [c.__enter__() for c in ctxs]


def _exit(ctxs):
    for c in ctxs:
        c.__exit__(None, None, None)


def _call(args: dict, *, trusted: bool = True) -> dict:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.icloud_actions import mail_reply_link

    caller = ToolCaller(agent="inbox", area="inbox") if trusted else None
    with caller_scope(caller):
        return asyncio.run(mail_reply_link(args))


def test_hma_branch_pushes_two_threaded_messages(parsed_parent) -> None:
    notifier_mock = MagicMock()
    notifier_mock.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "100.001", "group_id": "g"},
        {"status": "sent", "id": "100.002", "group_id": "g"},
    ])
    ctxs = _patches(parsed_parent(), notifier_mock)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "31551", "body": "Hallo,\n\nbezahlt bald.",
            "rationale": "test", "area": "ops", "agent": "alfred",
        })
    finally:
        _exit(ctxs)

    assert notifier_mock.send.await_count == 2
    first_call = notifier_mock.send.await_args_list[0]
    assert first_call.kwargs["area"] == "inbox"
    assert first_call.kwargs["agent"] == "inbox"
    second_call = notifier_mock.send.await_args_list[1]
    assert second_call.kwargs["quote_timestamp"] == "100.001"
    text = result["content"][0]["text"]
    assert "alice@icloud.com" in text
    assert "100.001" in text and "100.002" in text


def test_hma_branch_uses_plain_text_for_hostile_metadata_and_body(parsed_parent) -> None:
    notifier_mock = MagicMock()
    notifier_mock.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "100.001", "group_id": "g"},
        {"status": "sent", "id": "100.002", "group_id": "g"},
    ])
    parent = parsed_parent(
        from_="Attacker <https://evil.example|click>\nFrom: forged",
        subject="``` <!channel>\nUID: forged",
        x_icloud_hme="p=<https://evil.example|hma>; d=example",
    )
    ctxs = _patches(parent, notifier_mock)
    _enter(*ctxs)
    body = "Hello\n```\n<!channel> <https://evil.example|click>\n```\nBye"
    try:
        _call({
            "parent_uid": "31551\nFrom: forged",
            "parent_mailbox": "INBOX\nSubject: forged",
            "body": body,
            "rationale": "test",
        })
    finally:
        _exit(ctxs)

    context_call, body_call = notifier_mock.send.await_args_list
    assert context_call.kwargs["plain_text"] is True
    assert body_call.kwargs["plain_text"] is True
    assert context_call.args[0] == (
        "📧 Reply on HMA thread\n"
        "Arrived at: [<https://evil.example|hma>]\n"
        "From: [Attacker <https://evil.example|click> From: forged]\n"
        "Subject: [Re: ``` <!channel> UID: forged]\n"
        "Mailbox: [INBOX Subject: forged]\n"
        "UID: [31551 From: forged]\n\n"
        "⚠️ Open Mail.app on this thread → tap Reply "
        "(Mail.app auto-sets From to the HMA).\n"
        "Reply text in 👇"
    )
    assert body_call.args[0] == body


def test_hma_branch_requires_trusted_caller(parsed_parent) -> None:
    ctxs = _patches(parsed_parent())
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "31551", "body": "x", "rationale": "test",
        }, trusted=False)
    finally:
        _exit(ctxs)
    text = result["content"][0]["text"]
    assert "ERROR" in text
    assert "alice@icloud.com" in text
    assert "trusted caller context" in text.lower()


def test_hma_branch_ctx_send_failure_short_circuits(parsed_parent) -> None:
    notifier_mock = MagicMock()
    notifier_mock.send = AsyncMock(side_effect=RuntimeError("slack 500"))
    ctxs = _patches(parsed_parent(), notifier_mock)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "31551", "body": "x", "rationale": "t",
            "area": "ops", "agent": "alfred",
        })
    finally:
        _exit(ctxs)
    text = result["content"][0]["text"]
    assert "context push failed" in text
    assert notifier_mock.send.await_count == 1


def test_hma_branch_bare_send_failure_posts_warning_in_thread(parsed_parent) -> None:
    notifier_mock = MagicMock()
    notifier_mock.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "100.001", "group_id": "g"},
        RuntimeError("slack 500"),
        {"status": "sent", "id": "100.003", "group_id": "g"},
    ])
    ctxs = _patches(parsed_parent(), notifier_mock)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "31551", "body": "x", "rationale": "t",
            "area": "ops", "agent": "alfred",
        })
    finally:
        _exit(ctxs)
    assert notifier_mock.send.await_count == 3
    warn_call = notifier_mock.send.await_args_list[2]
    assert warn_call.kwargs["quote_timestamp"] == "100.001"
    warn_text = warn_call.args[0]
    assert "failed" in warn_text.lower()
    text = result["content"][0]["text"]
    assert "bare-reply push failed" in text


def test_hma_branch_failed_body_result_is_not_reported_as_success(parsed_parent) -> None:
    notifier_mock = MagicMock()
    notifier_mock.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "100.001", "group_id": "g"},
        {"status": "failed", "error": "slack 500", "group_id": "g"},
        {"status": "sent", "id": "100.003", "group_id": "g"},
    ])
    ctxs = _patches(parsed_parent(), notifier_mock)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "31551", "body": "x", "rationale": "t",
            "area": "ops", "agent": "alfred",
        })
    finally:
        _exit(ctxs)

    assert notifier_mock.send.await_count == 3
    warning_call = notifier_mock.send.await_args_list[2]
    assert warning_call.kwargs["quote_timestamp"] == "100.001"
    text = result["content"][0]["text"]
    assert text.startswith("ERROR:")
    assert "not delivered" in text
    assert "HMA reply pushed" not in text


def test_non_hma_returns_mailto_text(parsed_parent) -> None:
    parent = parsed_parent(
        recipients=("owner@example.com",),
        from_="Recruiter <r@firm.example.com>",
        subject="Re: position",
    )
    ctxs = _patches(parent)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "999", "body": "Thanks!", "rationale": "test",
        })
    finally:
        _exit(ctxs)
    text = result["content"][0]["text"]
    assert "mailto" in text


def test_parent_fetch_failure_returns_error() -> None:
    ctxs = _patches(None)
    _enter(*ctxs)
    try:
        result = _call({
            "parent_uid": "404", "body": "x", "rationale": "t",
        })
    finally:
        _exit(ctxs)
    text = result["content"][0]["text"]
    assert "ERROR" in text and "not fetchable" in text
