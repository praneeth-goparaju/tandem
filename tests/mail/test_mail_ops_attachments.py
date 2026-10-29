import asyncio
import contextlib
from email.message import EmailMessage

import pytest

from src.brain.mcp.tools import mail_ops
from src.brain.mcp.tools._mail_providers import MailProvider


def _msg_with_pdf() -> bytes:
    m = EmailMessage()
    m["From"] = "a@example.com"; m["Subject"] = "doc"
    m.set_content("see attached")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def _msg_with_text(content: str = "safe text") -> bytes:
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["Subject"] = "notes"
    m.set_content("see attached")
    m.add_attachment(content, subtype="plain", filename="notes.txt")
    return m.as_bytes()


def _install_fake(monkeypatch, raw: bytes, *, declared_size: int | None = None) -> None:
    class FakeIMAP:
        def select(self, mailbox, readonly=True): return ("OK", [b"1"])
        def uid(self, *a):
            size = len(raw) if declared_size is None else declared_size
            meta = f"1 (RFC822.SIZE {size} BODY[]<0>)".encode()
            return ("OK", [(meta, raw)])

    @contextlib.contextmanager
    def fake_session(provider, readonly=True):
        yield FakeIMAP()

    monkeypatch.setattr(mail_ops, "_imap_session", fake_session)
    monkeypatch.setattr(mail_ops, "_get_provider",
                        lambda n: MailProvider(n, "h", 993, "u", "p", "D", "A", n == "gmail"))


@pytest.fixture
def fake(monkeypatch):
    _install_fake(monkeypatch, _msg_with_pdf())


def test_list_attachments(fake):
    atts = asyncio.run(mail_ops.list_attachments("gmail", "1"))
    assert len(atts) == 1
    assert atts[0]["filename"] == "report.pdf"
    assert atts[0]["content_type"] == "application/pdf"


def test_get_attachment_rejects_binary_content(fake):
    with pytest.raises(RuntimeError, match="not safe for inline text reading"):
        asyncio.run(mail_ops.get_attachment("gmail", "1", 0))


def test_get_attachment_returns_text_in_memory(monkeypatch):
    _install_fake(monkeypatch, _msg_with_text("hello from attachment"))
    out = asyncio.run(mail_ops.get_attachment("gmail", "123", 0))
    assert out == {
        "filename": "notes.txt",
        "content_type": "text/plain",
        "size": len(b"hello from attachment") + 1,
        "content": "hello from attachment\n",
    }


@pytest.mark.parametrize("uid", ["1:*", "1,2", "1:5", "0", "-1"])
def test_singular_attachment_paths_reject_sequence_sets(fake, uid):
    with pytest.raises(ValueError, match="one positive numeric IMAP UID"):
        asyncio.run(mail_ops.list_attachments("gmail", uid))
    with pytest.raises(ValueError, match="one positive numeric IMAP UID"):
        asyncio.run(mail_ops.get_attachment("gmail", uid, 0))


def test_inline_text_attachment_size_is_capped(monkeypatch):
    _install_fake(monkeypatch, _msg_with_text("x" * 64))
    monkeypatch.setattr(mail_ops, "MAX_ATTACHMENT_BYTES", 16)
    with pytest.raises(RuntimeError, match="attachment too large"):
        asyncio.run(mail_ops.get_attachment("gmail", "1", 0))


def test_declared_message_size_is_capped_before_parsing(monkeypatch):
    _install_fake(
        monkeypatch,
        _msg_with_pdf(),
        declared_size=mail_ops.MAX_RAW_MESSAGE_BYTES + 1,
    )

    with pytest.raises(RuntimeError, match="message too large"):
        asyncio.run(mail_ops.list_attachments("gmail", "1"))


def test_raw_body_is_capped_when_declared_size_is_inaccurate(monkeypatch):
    raw = b"x" * 17
    _install_fake(monkeypatch, raw, declared_size=16)
    monkeypatch.setattr(mail_ops, "MAX_RAW_MESSAGE_BYTES", 16)

    with pytest.raises(RuntimeError, match="message too large"):
        asyncio.run(mail_ops.list_attachments("gmail", "1"))


def test_too_many_attachments_are_rejected(monkeypatch):
    msg = EmailMessage()
    msg.set_content("body")
    for index in range(mail_ops.MAX_ATTACHMENTS + 1):
        msg.add_attachment(
            str(index).encode(),
            maintype="application",
            subtype="octet-stream",
            filename=f"{index}.bin",
        )
    _install_fake(monkeypatch, msg.as_bytes())

    with pytest.raises(RuntimeError, match="too many attachments"):
        asyncio.run(mail_ops.list_attachments("gmail", "1"))
