from __future__ import annotations

from email.message import EmailMessage

from src.brain.mcp.tools._mail_quote import (
    decode_header,
    extract_text_body,
    pick_hma_recipient,
    quote_lines,
)


def test_third_party_hma_at_icloud_is_detected(parsed_parent) -> None:
    p = parsed_parent(recipients=("alice@icloud.com",))
    assert pick_hma_recipient(p, ["me@icloud.com"]) == "alice@icloud.com"


def test_privaterelay_address_is_always_hma(parsed_parent) -> None:
    p = parsed_parent(recipients=("someone@privaterelay.appleid.com",))
    assert pick_hma_recipient(p, []) == "someone@privaterelay.appleid.com"


def test_owned_alias_is_not_hma(parsed_parent) -> None:
    p = parsed_parent(recipients=("me@icloud.com",))
    assert pick_hma_recipient(p, ["me@icloud.com"]) is None


def test_owned_alias_match_is_case_insensitive(parsed_parent) -> None:
    p = parsed_parent(recipients=("Me@iCloud.com",))
    assert pick_hma_recipient(p, ["me@icloud.com"]) is None


def test_x_icloud_hme_header_wins_over_recipients(parsed_parent) -> None:
    p = parsed_parent(
        recipients=("me@icloud.com",),
        x_icloud_hme="p=bob@icloud.com; d=bar.com",
    )
    assert pick_hma_recipient(p, ["me@icloud.com"]) == "bob@icloud.com"


def test_x_icloud_hme_with_empty_alias_falls_through_to_recipients(parsed_parent) -> None:
    p = parsed_parent(
        recipients=("user@icloud.com",),
        x_icloud_hme="p=; d=foo.com",
    )
    assert pick_hma_recipient(p, []) == "user@icloud.com"


def test_mixed_recipients_picks_the_hma(parsed_parent) -> None:
    p = parsed_parent(recipients=("me@icloud.com", "alice@icloud.com"))
    assert pick_hma_recipient(p, ["me@icloud.com"]) == "alice@icloud.com"


def test_no_recipients_no_header_returns_none(parsed_parent) -> None:
    p = parsed_parent(recipients=())
    assert pick_hma_recipient(p, ["me@icloud.com"]) is None


def test_non_apple_recipient_is_not_hma(parsed_parent) -> None:
    p = parsed_parent(recipients=("someone@gmail.com",))
    assert pick_hma_recipient(p, []) is None


def test_privaterelay_short_circuits_owned_check(parsed_parent) -> None:
    p = parsed_parent(recipients=("test@privaterelay.appleid.com",))
    assert pick_hma_recipient(p, ["test@privaterelay.appleid.com"]) == "test@privaterelay.appleid.com"


def test_extract_text_body_plain_only() -> None:
    msg = EmailMessage()
    msg.set_content("hello world")
    assert extract_text_body(msg) == "hello world"


def test_extract_text_body_html_fallback_strips_tags() -> None:
    msg = EmailMessage()
    msg.set_content("placeholder")
    msg.add_alternative("<p>hi <b>there</b></p>", subtype="html")
    msg.clear_content()
    msg.set_content("placeholder")
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    multi = MIMEMultipart("alternative")
    multi.attach(MIMEText("<p>hi <b>there</b></p>", "html"))
    assert extract_text_body(multi) == "hi there"


def test_extract_text_body_multipart_prefers_plain() -> None:
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    multi = MIMEMultipart("alternative")
    multi.attach(MIMEText("PLAIN", "plain"))
    multi.attach(MIMEText("<p>HTML</p>", "html"))
    assert extract_text_body(multi) == "PLAIN"


def test_extract_text_body_empty_message() -> None:
    msg = EmailMessage()
    msg.set_content("")
    assert extract_text_body(msg) == ""


def test_quote_lines_prefixes_every_line() -> None:
    out = quote_lines("a\nb\nc")
    assert out == "> a\n> b\n> c"


def test_quote_lines_blank_lines_become_bare_gt() -> None:
    out = quote_lines("a\n\nb")
    assert out == "> a\n>\n> b"


def test_quote_lines_truncates_with_marker() -> None:
    text = "x" * 5000
    out = quote_lines(text, max_chars=100)
    assert "[truncated]" in out
    assert out.count("[truncated]") == 1


def test_quote_lines_empty_input_returns_empty() -> None:
    assert quote_lines("") == ""


def test_decode_header_rfc2047_round_trip() -> None:
    encoded = "=?UTF-8?Q?Re=3A_Mahnung?="
    assert decode_header(encoded) == "Re: Mahnung"


def test_decode_header_passes_through_plain_ascii() -> None:
    assert decode_header("Hello") == "Hello"


def test_decode_header_handles_none() -> None:
    assert decode_header(None) == ""
