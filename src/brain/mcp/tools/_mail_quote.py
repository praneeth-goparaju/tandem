from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass

from src.brain.mcp.tools._icloud import (
    imap_session,
)
from src.brain.mcp.tools.mail_ops import decode_header, fetch_raw_message

_log = logging.getLogger(__name__)

_APPLE_MAIL_DOMAINS = ("@icloud.com", "@me.com", "@mac.com")
_PRIVATERELAY_DOMAIN = "@privaterelay.appleid.com"
_HME_P_RE = re.compile(r"\bp=([^;\s]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedParent:
    msgid: str
    references: str
    from_: str
    subject: str
    date: str
    body: str
    recipients: tuple[str, ...]
    x_icloud_hme: str


def _decoded_payload(part: email.message.Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.encode(charset, errors="replace")
        except LookupError:
            return payload.encode("utf-8", errors="replace")
    return b""


def extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain, html = [], []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            payload = _decoded_payload(part)
            charset = part.get_content_charset() or "utf-8"
            try:
                txt = payload.decode(charset, errors="replace")
            except LookupError:
                txt = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain.append(txt)
            elif ctype == "text/html":
                html.append(txt)
        if plain:
            return "\n".join(plain).strip()
        if html:
            stripped = re.sub(r"<[^>]+>", " ", "\n".join(html))
            return re.sub(r"\s+", " ", stripped).strip()
        return ""
    payload = _decoded_payload(msg)
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except LookupError:
        return payload.decode("utf-8", errors="replace").strip()


def quote_lines(text: str, max_chars: int = 4000) -> str:
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def fetch_parent_for_quote(mailbox: str, uid: str) -> ParsedParent | None:
    """Fetch the parent message and return parsed fields for replying.

    `recipients` is the deduped tuple of addresses pulled from the parent's
    Delivered-To, X-Original-To, To, and Cc headers. Returns None on any
    fetch / SELECT / decode failure (logged at warn).

    The shared session reads credentials and endpoint configuration from the
    iCloud integration and guarantees cleanup.
    """
    try:
        with imap_session() as m:
            raw = fetch_raw_message(m, uid, mailbox)
    except (imaplib.IMAP4.error, OSError, RuntimeError, ssl.SSLError, ValueError) as e:
        _log.warning(
            "fetch_parent_for_quote failed for %s/%s: %s: %s",
            mailbox, uid, type(e).__name__, e,
        )
        return None

    parsed = email.message_from_bytes(raw)

    recipients: list[str] = []
    for h in ("Delivered-To", "X-Original-To", "To", "Cc"):
        for raw_val in parsed.get_all(h) or []:
            for _name, addr in email.utils.getaddresses([raw_val]):
                if addr and addr.lower() not in (a.lower() for a in recipients):
                    recipients.append(addr)

    return ParsedParent(
        msgid=(parsed.get("Message-ID") or "").strip(),
        references=(parsed.get("References") or "").strip(),
        from_=decode_header(parsed.get("From")).strip(),
        subject=decode_header(parsed.get("Subject")).strip(),
        date=(parsed.get("Date") or "").strip(),
        body=extract_text_body(parsed),
        recipients=tuple(recipients),
        x_icloud_hme=(parsed.get("X-Icloud-Hme") or "").strip(),
    )


def pick_hma_recipient(parent: ParsedParent, owned_aliases: Iterable[str]) -> str | None:
    """Return the HMA address the parent arrived at, or None.

    `owned_aliases` are the user's known send-from identities (primary
    iCloud address, custom-domain aliases, traditional iCloud aliases).
    HMAs are by definition NOT in that list — they're auto-generated.
    """
    owned_lower = {a.lower() for a in owned_aliases}

    if parent.x_icloud_hme:
        m = _HME_P_RE.search(parent.x_icloud_hme)
        if m:
            alias = m.group(1).strip()
            if alias:
                return alias

    for addr in parent.recipients:
        addr_lower = addr.lower()
        if addr_lower.endswith(_PRIVATERELAY_DOMAIN):
            return addr
        if addr_lower in owned_lower:
            continue
        if any(addr_lower.endswith(d) for d in _APPLE_MAIL_DOMAINS):
            return addr

    return None
