import asyncio

from src.brain.actions.handlers import mail_archive_batch
from src.brain.mcp.tools import mail_ops

SNAPSHOT = {"uidvalidity": "7", "message_id": "<m@x>", "message_from": "a", "message_subject": "s",
            "message_header_sha256": "a" * 64}


def batch(*uids, provider="icloud"):
    return {"summary": "s", "messages": [{"provider": provider, "mailbox": "INBOX", "uid": u, **SNAPSHOT} for u in uids]}


def run(monkeypatch, payload, fail=None):
    calls = []

    async def fake_archive_messages(provider, mailbox, items):
        calls.append((provider, mailbox, [uid for uid, _ in items]))
        assert all(snapshot == SNAPSHOT for _, snapshot in items)
        return [(uid, (fail or {}).get(uid, f"archived uid={uid}")) for uid, _ in items]

    monkeypatch.setattr(mail_ops, "archive_messages", fake_archive_messages)
    return asyncio.run(mail_archive_batch.execute(payload)), calls


def test_every_mail_in_the_batch_goes_in_one_call_per_account(monkeypatch):
    payload = batch("1", "2")
    payload["messages"] += batch("9", provider="gmail")["messages"]
    result, calls = run(monkeypatch, payload)
    assert result == {"ok": True, "result": "archived 3 of 3"}
    assert calls == [("icloud", "INBOX", ["1", "2"]), ("gmail", "INBOX", ["9"])]


def test_a_mail_that_changed_is_skipped_and_the_rest_still_go(monkeypatch):
    result, _ = run(monkeypatch, batch("1", "2", "3"), fail={"2": ValueError("mail message changed after approval")})
    assert result["partial"] and "archived 2 of 3" in result["result"] and "uid=2" in result["result"]


def test_an_unknown_outcome_is_reported_as_ambiguous(monkeypatch):
    result, _ = run(monkeypatch, batch("1", "2"), fail={"1": mail_ops.MailArchiveAmbiguousError("timeout")})
    assert not result["ok"] and "outcome unknown" in result["result"]


def test_an_empty_or_oversized_batch_is_refused(monkeypatch):
    for payload in ({"summary": "s", "messages": []}, batch(*[str(n) for n in range(51)])):
        result, calls = run(monkeypatch, payload)
        assert not result["ok"] and calls == []


def test_a_batch_gets_five_minutes_and_everything_else_one():
    from src.brain.actions.executor import handler_timeout

    assert handler_timeout("mail_archive_batch") == 300.0
    assert handler_timeout("mail_archive") == 60.0
