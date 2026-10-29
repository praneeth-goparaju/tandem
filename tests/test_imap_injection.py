from __future__ import annotations

import pytest

from src.brain.mcp.tools._icloud import escape_imap_str, validate_mailbox_name


def test_escape_imap_str_passes_clean_strings() -> None:
    assert escape_imap_str("inbox") == "inbox"
    assert escape_imap_str("subject keyword") == "subject keyword"


def test_escape_imap_str_escapes_quote_and_backslash() -> None:
    assert escape_imap_str('foo"bar') == 'foo\\"bar'
    assert escape_imap_str("foo\\bar") == "foo\\\\bar"


@pytest.mark.parametrize(
    "bad",
    [
        "foo\r\nLOGOUT",
        "foo\nbar",
        "foo\rbar",
        "foo\x00bar",
    ],
)
def test_escape_imap_str_rejects_control_chars(bad: str) -> None:
    with pytest.raises(ValueError):
        escape_imap_str(bad)


def test_escape_imap_str_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        escape_imap_str(b"bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        '"',
        "\\",
        "INBOX\r",
        "INBOX\n",
        "INBOX\x00",
        'INBOX" UID FETCH 1:*',
        "",
    ],
)
def test_validate_mailbox_name_rejects_dangerous(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_mailbox_name(bad)


def test_validate_mailbox_name_passes_safe() -> None:
    assert validate_mailbox_name("INBOX") == "INBOX"
    assert validate_mailbox_name("Sent Messages") == "Sent Messages"
    assert validate_mailbox_name("Travel/2026") == "Travel/2026"
