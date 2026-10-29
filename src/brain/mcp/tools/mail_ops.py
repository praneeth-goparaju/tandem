"""Provider-aware core IMAP operations: archive and bounded attachments.

Async functions run blocking IMAP work in worker threads and raise on failure.
MCP tools and action handlers wrap these operations and convert exceptions to
their own result shapes. Archiving on Gmail moves the message from INBOX to
All Mail; on iCloud it moves the message to the Archive mailbox.
"""
from __future__ import annotations

import asyncio
import email as _email
import hashlib
import imaplib
import re as _re

from src.brain.mcp.tools._icloud import (
    validate_mailbox_name,
    validate_single_uid,
)
from src.brain.mcp.tools._mail_providers import (
    get_provider as _get_provider,
)
from src.brain.mcp.tools._mail_providers import (
    imap_session as _imap_session,
)

MAX_SNAPSHOT_HEADER_BYTES = 16 * 1024
MAIL_SNAPSHOT_KEYS = (
    "uidvalidity",
    "message_id",
    "message_from",
    "message_subject",
    "message_header_sha256",
)


class MailArchiveAmbiguousError(RuntimeError):
    """The archive command may have committed; automatic replay is unsafe."""


class MailArchivePartialError(RuntimeError):
    """An archive copy committed but inbox removal did not fully complete."""


def decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 header, retaining undecodable input as data."""
    if not raw:
        return ""
    try:
        return str(_email.header.make_header(_email.header.decode_header(raw)))
    except Exception:  # noqa: BLE001
        return raw


def _quote(name: str) -> str:
    return f'"{name}"'


def _select_mailbox(
    m: imaplib.IMAP4_SSL, mailbox: str, *, readonly: bool
) -> None:
    status, _ = m.select(_quote(mailbox), readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"mailbox {mailbox!r} not found")


def _selected_uidvalidity(m: imaplib.IMAP4_SSL) -> str:
    _code, values = m.response("UIDVALIDITY")
    raw = values[-1] if values else b""
    text = raw.decode("ascii", errors="strict") if isinstance(raw, bytes) else str(raw)
    match = _re.search(r"\d+", text)
    if not match or int(match.group()) <= 0:
        raise RuntimeError("mailbox did not provide a valid UIDVALIDITY")
    return match.group()


def _selected_mail_snapshot(m: imaplib.IMAP4_SSL, uid: str) -> dict[str, str]:
    uid_b = validate_single_uid(uid)
    status, data = m.uid(
        "FETCH",
        uid_b,
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]"
        f"<0.{MAX_SNAPSHOT_HEADER_BYTES + 1}>)",
    )
    if status != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"message uid={uid} not found")
    raw = data[0][1]
    if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT_HEADER_BYTES:
        raise RuntimeError("message identity headers exceed the snapshot limit")
    msg = _email.message_from_bytes(raw)

    def one_line(value: str | None) -> str:
        return " ".join(decode_header(value).split())[:500]

    return {
        "uidvalidity": _selected_uidvalidity(m),
        "message_id": one_line(msg.get("Message-ID")),
        "message_from": one_line(msg.get("From")),
        "message_subject": one_line(msg.get("Subject")),
        "message_header_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _snapshot_message_sync(
    provider_name: str, uid: str, mailbox: str = "INBOX"
) -> dict[str, str]:
    uid = validate_single_uid(uid)
    mailbox = validate_mailbox_name(mailbox)
    provider = _get_provider(provider_name)
    with _imap_session(provider) as m:
        _select_mailbox(m, mailbox, readonly=True)
        return _selected_mail_snapshot(m, uid)


async def snapshot_message(
    provider_name: str, uid: str, mailbox: str = "INBOX"
) -> dict[str, str]:
    """Freeze stable IMAP identity and review headers before approval."""
    return await asyncio.to_thread(_snapshot_message_sync, provider_name, uid, mailbox)


def _archive_message_sync(
    provider_name: str,
    uid: str,
    mailbox: str = "INBOX",
    expected_snapshot: dict[str, str] | None = None,
) -> str:
    mailbox = validate_mailbox_name(mailbox)
    provider = _get_provider(provider_name)
    with _imap_session(provider) as m:
        _select_mailbox(m, mailbox, readonly=False)
        return _archive_selected(m, provider, provider_name, uid, mailbox, expected_snapshot)


def _archive_messages_sync(
    provider_name: str, mailbox: str, items: list[tuple[str, dict[str, str] | None]],
) -> list[tuple[str, str | Exception]]:
    """Archive several messages over one IMAP session; each gets its own result or error.

    A failure of one message does not stop the others; an error that may
    have left the session unusable ends the batch, and the rest are reported
    as not tried.
    """
    mailbox = validate_mailbox_name(mailbox)
    provider = _get_provider(provider_name)
    results: list[tuple[str, str | Exception]] = []
    with _imap_session(provider) as m:
        for index, (uid, expected_snapshot) in enumerate(items):
            try:
                # The server reports UIDVALIDITY when a mailbox is selected and imaplib
                # hands it out once, so each message's snapshot check selects again.
                _select_mailbox(m, mailbox, readonly=False)
                results.append((uid, _archive_selected(m, provider, provider_name, uid, mailbox, expected_snapshot)))
            except (ValueError, RuntimeError) as exc:
                results.append((uid, exc))
                if isinstance(exc, (MailArchiveAmbiguousError, MailArchivePartialError)):
                    results += [(rest, RuntimeError("not tried")) for rest, _ in items[index + 1:]]
                    break
    return results


async def archive_messages(
    provider_name: str, mailbox: str, items: list[tuple[str, dict[str, str] | None]],
) -> list[tuple[str, str | Exception]]:
    """Archive several messages over one IMAP session, off the event loop."""
    return await asyncio.to_thread(_archive_messages_sync, provider_name, mailbox, items)


def _archive_selected(
    m, provider, provider_name: str, uid: str, mailbox: str,
    expected_snapshot: dict[str, str] | None,
) -> str:
    """Archive one message in the mailbox already selected read-write on `m`."""
    uid_b = validate_single_uid(uid)
    if expected_snapshot is not None:
        actual = _selected_mail_snapshot(m, uid_b)
        if actual != expected_snapshot:
            raise ValueError("mail message changed after approval; review it again")
    if provider.uses_gmail_labels and mailbox.upper() == "INBOX":
        # Gmail never lists the selected mailbox's own label in X-GM-LABELS, so
        # removing \\Inbox while INBOX is selected answers OK and changes nothing.
        # Moving the message to All Mail is what takes it out of the inbox.
        try:
            status, data = m.uid("MOVE", uid_b, _quote(provider.archive_mailbox))
        except Exception as exc:  # noqa: BLE001
            raise MailArchiveAmbiguousError(
                "Gmail UID MOVE outcome is unknown; reconcile the mailbox manually"
            ) from exc
        if status != "OK":
            raise RuntimeError(f"MOVE to {provider.archive_mailbox} failed: {data!r}")
        status, data = m.uid("SEARCH", "UID", uid_b)
        if status == "OK" and data and data[0] and data[0].split():
            raise RuntimeError(f"message uid={uid} is still in {mailbox} after MOVE")
    elif provider.uses_gmail_labels:
        try:
            status, data = m.uid("STORE", uid_b, "-X-GM-LABELS", "(\\Inbox)")
        except Exception as exc:  # noqa: BLE001
            raise MailArchiveAmbiguousError(
                "Gmail label removal outcome is unknown; reconcile the mailbox manually"
            ) from exc
        if status != "OK":
            raise RuntimeError(f"remove \\Inbox failed: {data!r}")
    else:
        capabilities = {
            item.decode().upper() if isinstance(item, bytes) else str(item).upper()
            for item in getattr(m, "capabilities", ())
        }
        if "MOVE" in capabilities:
            try:
                status, data = m.uid("MOVE", uid_b, _quote(provider.archive_mailbox))
            except Exception as exc:  # noqa: BLE001
                raise MailArchiveAmbiguousError(
                    "UID MOVE outcome is unknown; reconcile the mailbox manually"
                ) from exc
            if status != "OK":
                raise RuntimeError(f"MOVE to Archive failed: {data!r}")
            return f"archived uid={uid} ({provider_name}, UID MOVE)"
        try:
            status, data = m.uid("COPY", uid_b, _quote(provider.archive_mailbox))
        except Exception as exc:  # noqa: BLE001
            raise MailArchiveAmbiguousError(
                "COPY outcome is unknown; reconcile Archive before retrying"
            ) from exc
        if status != "OK":
            raise RuntimeError(f"COPY to Archive failed: {data!r}")
        try:
            status, data = m.uid("STORE", uid_b, "+FLAGS", "(\\Deleted)")
        except Exception as exc:  # noqa: BLE001
            raise MailArchivePartialError(
                "message copied to Archive; inbox removal outcome is unknown"
            ) from exc
        if status != "OK":
            raise MailArchivePartialError(
                f"message copied to Archive but STORE \\Deleted failed: {data!r}"
            )
        try:
            status, data = m.uid("EXPUNGE", uid_b)
        except Exception as exc:  # noqa: BLE001
            raise MailArchivePartialError(
                "message copied and flagged deleted; expunge outcome is unknown"
            ) from exc
        if status != "OK":
            # Server lacks UIDPLUS UID EXPUNGE. The message is already safely COPYed to
            # Archive and flagged \Deleted; do NOT fall back to unscoped expunge (it would
            # remove other \Deleted messages). Leave it for the client to expunge later.
            raise MailArchivePartialError(
                f"message copied and flagged deleted; UID EXPUNGE failed: {data!r}"
            )
    return f"archived uid={uid} ({provider_name})"


async def archive_message(
    provider_name: str,
    uid: str,
    mailbox: str = "INBOX",
    expected_snapshot: dict[str, str] | None = None,
) -> str:
    """Archive a message without running blocking IMAP calls on the event loop."""
    return await asyncio.to_thread(
        _archive_message_sync, provider_name, uid, mailbox, expected_snapshot
    )


# --- attachment ops ---
MAX_ATTACHMENT_BYTES = 128 * 1024
MAX_ATTACHMENTS = 5
MAX_RAW_MESSAGE_BYTES = 32 * 1024 * 1024

_TEXT_ATTACHMENT_TYPES = frozenset({
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
})

_RFC822_SIZE_RE = _re.compile(rb"RFC822\.SIZE\s+(\d+)", _re.IGNORECASE)


def fetch_raw_message(m: imaplib.IMAP4_SSL, uid: str, mailbox: str) -> bytes:
    uid_b = validate_single_uid(uid)
    validate_mailbox_name(mailbox)
    _select_mailbox(m, mailbox, readonly=True)
    # Ask for at most cap+1 bytes and the authoritative message size in one
    # round trip. The extra byte lets us detect servers that omit RFC822.SIZE.
    status, data = m.uid(
        "FETCH",
        uid_b,
        f"(RFC822.SIZE BODY.PEEK[]<0.{MAX_RAW_MESSAGE_BYTES + 1}>)",
    )
    if status != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"message uid={uid} not found")
    meta, raw = data[0]
    size_match = _RFC822_SIZE_RE.search(meta if isinstance(meta, bytes) else b"")
    declared_size = int(size_match.group(1)) if size_match else None
    if declared_size is not None and declared_size > MAX_RAW_MESSAGE_BYTES:
        raise RuntimeError(
            f"message too large ({declared_size} bytes > {MAX_RAW_MESSAGE_BYTES})"
        )
    if not isinstance(raw, bytes):
        raise RuntimeError(f"message uid={uid} returned a non-bytes body")
    if len(raw) > MAX_RAW_MESSAGE_BYTES:
        raise RuntimeError(
            f"message too large ({len(raw)} bytes > {MAX_RAW_MESSAGE_BYTES})"
        )
    return raw


def _attachment_parts(msg) -> list:
    parts = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            parts.append(part)
            if len(parts) > MAX_ATTACHMENTS:
                raise RuntimeError(
                    f"too many attachments ({len(parts)} > {MAX_ATTACHMENTS})"
                )
    return parts


def _list_attachments_sync(provider_name: str, uid: str, mailbox: str = "INBOX") -> list[dict]:
    provider = _get_provider(provider_name)
    with _imap_session(provider) as m:
        raw = fetch_raw_message(m, uid, mailbox)
    msg = _email.message_from_bytes(raw)
    out = []
    for i, part in enumerate(_attachment_parts(msg)):
        payload = part.get_payload(decode=True) or b""
        out.append({
            "index": i,
            "filename": part.get_filename() or f"attachment_{i}",
            "content_type": part.get_content_type(),
            "size": len(payload),
        })
    return out


async def list_attachments(provider_name: str, uid: str, mailbox: str = "INBOX") -> list[dict]:
    """List attachments without running blocking IMAP calls on the event loop."""
    return await asyncio.to_thread(_list_attachments_sync, provider_name, uid, mailbox)


def _get_attachment_sync(
    provider_name: str, uid: str, index: int, mailbox: str = "INBOX"
) -> dict:
    provider = _get_provider(provider_name)
    with _imap_session(provider) as m:
        raw = fetch_raw_message(m, uid, mailbox)
    msg = _email.message_from_bytes(raw)
    parts = _attachment_parts(msg)
    if index < 0 or index >= len(parts):
        raise RuntimeError(f"attachment index {index} out of range ({len(parts)} found)")
    part = parts[index]
    payload = part.get_payload(decode=True) or b""
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8")
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise RuntimeError(f"attachment too large ({len(payload)} bytes > {MAX_ATTACHMENT_BYTES})")
    content_type = part.get_content_type().lower()
    if not content_type.startswith("text/") and content_type not in _TEXT_ATTACHMENT_TYPES:
        raise RuntimeError(
            f"attachment content type {content_type!r} is not safe for inline text reading"
        )
    charset = part.get_content_charset() or "utf-8"
    try:
        content = payload.decode(charset, errors="replace")
    except LookupError:
        content = payload.decode("utf-8", errors="replace")
    return {
        "filename": part.get_filename() or f"attachment_{index}",
        "content_type": content_type,
        "size": len(payload),
        "content": content,
    }


async def get_attachment(
    provider_name: str, uid: str, index: int, mailbox: str = "INBOX"
) -> dict:
    """Fetch a small text attachment without writing untrusted data to disk."""
    return await asyncio.to_thread(
        _get_attachment_sync,
        provider_name,
        uid,
        index,
        mailbox,
    )
