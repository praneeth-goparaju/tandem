import asyncio

import pytest

from src.brain.actions.handlers import mail_archive
from src.brain.mcp.tools import mail_ops

SNAPSHOT = {
    "uidvalidity": "7",
    "message_id": "<m@example.com>",
    "message_from": "sender@example.com",
    "message_subject": "Subject",
    "message_header_sha256": "a" * 64,
}


def test_archive_handler_ok(monkeypatch):
    async def fake(p, uid, mailbox="INBOX", expected_snapshot=None):
        assert expected_snapshot == SNAPSHOT
        return "archived"
    monkeypatch.setattr(mail_ops, "archive_message", fake)
    res = asyncio.run(mail_archive.execute({
        "provider": "gmail", "uid": "1", **SNAPSHOT,
    }))
    assert res == {"ok": True, "result": "archived"}


def test_archive_handler_missing_field():
    res = asyncio.run(mail_archive.execute({"provider": "gmail"}))
    assert res["ok"] is False


def test_archive_transport_failure_is_marked_retryable(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise TimeoutError("mail server timed out")

    monkeypatch.setattr(mail_ops, "archive_message", fail)
    payload = {"provider": "gmail", "uid": "1", **SNAPSHOT}

    res = asyncio.run(mail_archive.execute(payload))

    assert res["ok"] is False
    assert res["retryable"] is True


@pytest.mark.parametrize(
    ("error", "flag"),
    [
        (mail_ops.MailArchiveAmbiguousError("unknown"), "ambiguous"),
        (mail_ops.MailArchivePartialError("copied"), "partial"),
    ],
)
def test_archive_uncertain_effect_is_never_retryable(monkeypatch, error, flag):
    async def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(mail_ops, "archive_message", fail)
    res = asyncio.run(mail_archive.execute({
        "provider": "icloud", "uid": "1", **SNAPSHOT,
    }))

    assert res[flag] is True
    assert not res.get("retryable", False)


