from __future__ import annotations

import asyncio
import re
from collections.abc import Collection
from urllib.parse import urlsplit

_QUERY_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


async def read_bounded(stream, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes so callers can detect overflow."""
    try:
        return await stream.readexactly(max_bytes + 1)
    except asyncio.IncompleteReadError as exc:
        return exc.partial


def public_query_terms(value: object) -> frozenset[str]:
    return frozenset(
        word.casefold()
        for word in _QUERY_WORD_RE.findall(str(value or ""))
        if len(word) <= 64
    )


def safe_https_url(
    value: object, *, allowed_hosts: Collection[str] | None = None
) -> str | None:
    """Return a credential-free HTTPS/443 URL, optionally host-restricted."""
    text = str(value or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    text = text.strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (allowed_hosts is not None and parsed.hostname not in allowed_hosts)
    ):
        return None
    return text
