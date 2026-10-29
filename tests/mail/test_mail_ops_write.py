# tests/mail/test_mail_ops_write.py
import asyncio
import threading

import pytest

from src.brain.mcp.tools import mail_ops
from src.brain.mcp.tools._mail_providers import MailProvider


class FakeIMAP:
    def __init__(self):
        self.commands = []
        self.capabilities = ()
    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly)); return ("OK", [b"1"])
    def response(self, code):
        assert code == "UIDVALIDITY"
        return (code, [b"7"])
    def uid(self, *args):
        self.commands.append(("uid",) + args)
        if args[0] == "FETCH":
            return (
                "OK",
                [(b"1 (BODY[HEADER] {74})", (
                    b"From: Sender <sender@example.com>\r\n"
                    b"Subject: Hello\r\nMessage-ID: <m@example.com>\r\n\r\n"
                ))],
            )
        if args[0] == "EXPUNGE":
            return ("OK", [b""])
        return ("OK", [b""])
    def expunge(self):
        self.commands.append(("expunge",)); return ("OK", [b""])


@pytest.fixture
def fake(monkeypatch):
    f = FakeIMAP()
    import contextlib
    @contextlib.contextmanager
    def fake_session(provider, readonly=True):
        yield f
    monkeypatch.setattr(mail_ops, "_imap_session", fake_session)
    # gmail provider stub
    prov = MailProvider("gmail", "imap.gmail.com", 993, "u", "p",
                        "[Gmail]/Drafts", "[Gmail]/All Mail", True)
    monkeypatch.setattr(mail_ops, "_get_provider", lambda n: prov)
    return f


def test_gmail_archive_moves_inbox_message_to_all_mail(fake):
    asyncio.run(mail_ops.archive_message("gmail", "42"))
    # Removing \\Inbox while INBOX is selected is a silent no-op on Gmail.
    assert any(c[:2] == ("uid", "MOVE") and c[3] == '"[Gmail]/All Mail"' for c in fake.commands)
    assert not any(c[:2] == ("uid", "STORE") for c in fake.commands)


def test_gmail_archive_fails_when_message_stays_in_inbox(fake):
    original_uid = fake.uid

    def still_there(*args):
        if args[0] == "SEARCH":
            return ("OK", [b"42"])
        return original_uid(*args)

    fake.uid = still_there

    with pytest.raises(RuntimeError, match="still in INBOX"):
        asyncio.run(mail_ops.archive_message("gmail", "42"))


def test_gmail_archive_outside_inbox_removes_inbox_label(fake):
    asyncio.run(mail_ops.archive_message("gmail", "42", mailbox="[Gmail]/All Mail"))
    assert any(c[0] == "uid" and c[1] == "STORE" and c[3] == "-X-GM-LABELS" and "\\Inbox" in c[4]
               for c in fake.commands)


def test_gmail_lost_store_ack_is_terminal_ambiguous(fake):
    original_uid = fake.uid

    def lose_store_ack(*args):
        if args[0] == "MOVE":
            raise TimeoutError("connection lost after command dispatch")
        return original_uid(*args)

    fake.uid = lose_store_ack

    with pytest.raises(mail_ops.MailArchiveAmbiguousError):
        asyncio.run(mail_ops.archive_message("gmail", "42"))


def test_blocking_imap_body_runs_off_event_loop(monkeypatch):
    caller_thread = threading.get_ident()
    worker_threads = []

    def fake_archive(_provider, _uid, _mailbox, _snapshot):
        worker_threads.append(threading.get_ident())
        return "archived"

    monkeypatch.setattr(mail_ops, "_archive_message_sync", fake_archive)

    assert asyncio.run(mail_ops.archive_message("gmail", "42")) == "archived"
    assert worker_threads and worker_threads[0] != caller_thread


@pytest.fixture
def fake_icloud(monkeypatch):
    f = FakeIMAP()
    import contextlib

    @contextlib.contextmanager
    def fake_session(provider, readonly=True):
        yield f

    monkeypatch.setattr(mail_ops, "_imap_session", fake_session)
    prov = MailProvider(
        "icloud", "imap.mail.me.com", 993, "u", "p", "Drafts", "Archive", False
    )
    monkeypatch.setattr(mail_ops, "_get_provider", lambda n: prov)
    return f


def test_icloud_archive_copies_then_deletes(fake_icloud):
    asyncio.run(mail_ops.archive_message("icloud", "99"))
    f = fake_icloud
    assert any(c[0] == "uid" and c[1] == "COPY" and "Archive" in c[3] for c in f.commands)
    assert any(
        c[0] == "uid" and c[1] == "STORE" and "+FLAGS" in c[3] and "\\Deleted" in c[4]
        for c in f.commands
    )
    # UID-scoped expunge (UIDPLUS) — must NOT fall back to bare expunge
    assert any(c[0] == "uid" and c[1] == "EXPUNGE" for c in f.commands)
    assert not any(c[0] == "expunge" for c in f.commands)


def test_snapshot_binds_uidvalidity_and_review_headers(fake):
    snapshot = asyncio.run(mail_ops.snapshot_message("gmail", "42"))

    assert snapshot["uidvalidity"] == "7"
    assert snapshot["message_id"] == "<m@example.com>"
    assert snapshot["message_from"] == "Sender <sender@example.com>"
    assert snapshot["message_subject"] == "Hello"
    assert len(snapshot["message_header_sha256"]) == 64


def test_archive_rechecks_snapshot_before_any_effect(fake):
    expected = asyncio.run(mail_ops.snapshot_message("gmail", "42"))
    expected["message_subject"] = "Different message"

    with pytest.raises(ValueError, match="changed after approval"):
        asyncio.run(mail_ops.archive_message(
            "gmail", "42", expected_snapshot=expected,
        ))

    assert not any(
        command[0] == "uid" and command[1] == "STORE"
        for command in fake.commands
    )


def test_icloud_prefers_uid_move_when_supported(fake_icloud):
    fake_icloud.capabilities = (b"MOVE",)

    asyncio.run(mail_ops.archive_message("icloud", "99"))

    assert any(c[0] == "uid" and c[1] == "MOVE" for c in fake_icloud.commands)
    assert not any(c[0] == "uid" and c[1] == "COPY" for c in fake_icloud.commands)


def test_icloud_failure_after_copy_is_terminal_partial(fake_icloud):
    original_uid = fake_icloud.uid

    def fail_store(*args):
        if args[0] == "STORE":
            raise TimeoutError("connection lost")
        return original_uid(*args)

    fake_icloud.uid = fail_store
    with pytest.raises(mail_ops.MailArchivePartialError):
        asyncio.run(mail_ops.archive_message("icloud", "99"))


class OnceIMAP(FakeIMAP):
    """Like imaplib: UIDVALIDITY comes with SELECT and response() hands it out once."""

    def __init__(self):
        super().__init__()
        self.pending = False

    def select(self, mailbox, readonly=False):
        self.pending = True
        return super().select(mailbox, readonly)

    def response(self, code):
        assert code == "UIDVALIDITY"
        if not self.pending:
            return (code, [None])
        self.pending = False
        return (code, [b"7"])


def test_a_batch_archives_every_message_over_one_session(monkeypatch):
    import contextlib

    sessions = []

    @contextlib.contextmanager
    def counting_session(provider, readonly=True):
        sessions.append(OnceIMAP())
        yield sessions[-1]

    monkeypatch.setattr(mail_ops, "_imap_session", counting_session)
    prov = MailProvider("gmail", "imap.gmail.com", 993, "u", "p", "[Gmail]/Drafts", "[Gmail]/All Mail", True)
    monkeypatch.setattr(mail_ops, "_get_provider", lambda n: prov)

    expected = {"uidvalidity": "7", "message_id": "<m@example.com>", "message_from": "Sender <sender@example.com>",
                "message_subject": "Hello", "message_header_sha256": _header_sha()}
    real = [(uid, expected) for uid in ("41", "42", "43")]
    results = asyncio.run(mail_ops.archive_messages("gmail", "INBOX", real))
    assert not any(isinstance(outcome, Exception) for _, outcome in results), results

    assert len(sessions) == 1
    assert [uid for uid, _ in results] == ["41", "42", "43"]
    assert sum(1 for c in sessions[0].commands if c[:2] == ("uid", "MOVE")) == 3


def test_one_bad_message_does_not_stop_the_rest_of_the_batch(fake):
    results = mail_ops._archive_messages_sync("gmail", "INBOX", [("41", None), ("not-a-uid", None), ("43", None)])
    assert isinstance(results[1][1], ValueError)
    assert not isinstance(results[0][1], Exception) and not isinstance(results[2][1], Exception)


def _header_sha():
    import hashlib

    return hashlib.sha256(
        b"From: Sender <sender@example.com>\r\nSubject: Hello\r\nMessage-ID: <m@example.com>\r\n\r\n"
    ).hexdigest()
