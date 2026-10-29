from __future__ import annotations

from urllib.parse import quote

# iOS Mail.app silently truncates mailto: URLs past ~2500 chars, dropping reply
# content the user believes was sent. Cap below that and fail loudly instead.
_MAX_MAILTO_URL = 2400


def _q_query(s: str) -> str:
    return quote(s, safe="")


def _q_address(s: str) -> str:
    """Percent-encode an email address for a mailto URL.

    Per RFC 6068, the `@` and `.` in the address are STRUCTURAL and must
    remain literal. Without literal `@`, Slack/iOS link-detectors see
    the local part as a hostname and try to wrap it as `<http://…>`,
    breaking the link. Comma is also literal (multi-recipient separator).
    Keep them safe; encode everything else that's not an unreserved char.
    """
    return quote(s, safe="@.,_-+")


def build_mailto(
    to: list[str] | str,
    *,
    subject: str = "",
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | str | None = None,
) -> str:
    if isinstance(to, str):
        to_str = to.strip()
    else:
        to_str = ",".join(addr.strip() for addr in to if addr.strip())

    parts: list[str] = []
    if subject:
        parts.append(f"subject={_q_query(subject)}")
    if body:
        parts.append(f"body={_q_query(body)}")
    if cc:
        cc_list = cc if isinstance(cc, list) else [cc]
        cc_str = ",".join(a.strip() for a in cc_list if a.strip())
        if cc_str:
            parts.append(f"cc={_q_address(cc_str)}")
    if bcc:
        bcc_list = bcc if isinstance(bcc, list) else [bcc]
        bcc_str = ",".join(a.strip() for a in bcc_list if a.strip())
        if bcc_str:
            parts.append(f"bcc={_q_address(bcc_str)}")
    if in_reply_to:
        parts.append(f"in-reply-to={_q_query(in_reply_to)}")
    if references:
        refs_str = " ".join(references) if isinstance(references, list) else references
        parts.append(f"references={_q_query(refs_str)}")

    query = "&".join(parts)
    url = f"mailto:{_q_address(to_str)}" + (f"?{query}" if query else "")
    if len(url) > _MAX_MAILTO_URL:
        raise ValueError(
            f"mailto URL is {len(url)} chars — over the ~{_MAX_MAILTO_URL}-char limit iOS "
            "Mail silently truncates. Shorten the reply body."
        )
    return url
