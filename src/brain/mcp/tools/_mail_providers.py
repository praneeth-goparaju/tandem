from __future__ import annotations

import contextlib
import imaplib
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

from src.brain.mcp.tools._icloud import ICloudNotConfigured, require_creds
from src.orchestrator.config import (
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    ICLOUD_IMAP_USER,
)

NETWORK_TIMEOUT = 15.0


class MailNotConfigured(RuntimeError):
    ...


@dataclass(frozen=True)
class MailProvider:
    name: str
    imap_host: str
    imap_port: int
    login_user: str
    password: str
    drafts_mailbox: str
    archive_mailbox: str
    uses_gmail_labels: bool


def get_provider(
    name: str, *, apple_id: str | None = None, imap_user: str | None = None
) -> MailProvider:
    """The mail provider ``name``. For iCloud, ``apple_id`` picks an account other than the
    one in the environment; it then signs in as ``imap_user`` or else as ``apple_id``."""
    if apple_id is not None and name != "icloud":
        raise MailNotConfigured(f"only iCloud mail can use the account {apple_id}")
    if name == "icloud":
        named = apple_id is not None
        try:
            apple_id, app_password = require_creds(apple_id)
        except ICloudNotConfigured as e:
            raise MailNotConfigured(str(e)) from e
        # A named account never signs in with the environment's mailbox address.
        login_user = (imap_user or apple_id) if named else (ICLOUD_IMAP_USER or apple_id)
        return MailProvider(
            name="icloud",
            imap_host="imap.mail.me.com",
            imap_port=993,
            login_user=login_user,
            password=app_password,
            drafts_mailbox="Drafts",
            archive_mailbox="Archive",
            uses_gmail_labels=False,
        )
    if name == "gmail":
        if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
            raise MailNotConfigured("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
        return MailProvider(
            name="gmail",
            imap_host="imap.gmail.com",
            imap_port=993,
            login_user=GMAIL_ADDRESS,
            password=GMAIL_APP_PASSWORD,
            drafts_mailbox="[Gmail]/Drafts",
            archive_mailbox="[Gmail]/All Mail",
            uses_gmail_labels=True,
        )
    raise ValueError(f"unknown mail provider {name!r} (valid: icloud, gmail)")


@contextlib.contextmanager
def imap_session(provider: MailProvider) -> Iterator[imaplib.IMAP4_SSL]:
    client = imaplib.IMAP4_SSL(provider.imap_host, provider.imap_port, timeout=NETWORK_TIMEOUT)
    try:
        client.login(provider.login_user, provider.password)
        yield client
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError, ssl.SSLError):
            client.logout()
        with contextlib.suppress(OSError, ssl.SSLError):
            client.shutdown()
