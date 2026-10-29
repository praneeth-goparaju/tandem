from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from src.brain.mcp.tools._mailto import build_mailto


def _split(url: str) -> tuple[str, dict[str, list[str]]]:
    assert url.startswith("mailto:"), url
    rest = url[len("mailto:"):]
    if "?" in rest:
        path, query = rest.split("?", 1)
        return unquote(path), parse_qs(query)
    return unquote(path) if (path := rest) else "", {}


def test_single_address_keeps_at_and_dot_literal() -> None:
    url = build_mailto("alice@example.com")
    # Slack/iOS link detectors require literal @ . in the address part.
    assert "alice@example.com" in url
    assert "?" not in url


def test_list_addresses_join_with_literal_comma() -> None:
    url = build_mailto(["a@example.com", "b@example.org", " c@example.net "])
    assert "a@example.com,b@example.org,c@example.net" in url


def test_subject_special_chars_are_escaped() -> None:
    url = build_mailto("a@example.com", subject="Re: a&b?c=d")
    _, q = _split(url)
    assert q["subject"] == ["Re: a&b?c=d"]


def test_body_newlines_round_trip() -> None:
    url = build_mailto("a@example.com", body="line1\nline2")
    _, q = _split(url)
    assert q["body"] == ["line1\nline2"]


def test_cc_bcc_join_and_keep_at_literal() -> None:
    url = build_mailto("a@example.com", cc=["c1@example.com", "c2@example.com"], bcc=["b@example.com"])
    _, q = _split(url)
    assert q["cc"] == ["c1@example.com,c2@example.com"]
    assert q["bcc"] == ["b@example.com"]


def test_in_reply_to_with_angle_brackets_is_query_encoded() -> None:
    url = build_mailto("a@example.com", in_reply_to="<id@host>")
    _, q = _split(url)
    assert q["in-reply-to"] == ["<id@host>"]


def test_references_list_joined_with_space() -> None:
    url = build_mailto("a@example.com", references=["<a@x>", "<b@y>"])
    _, q = _split(url)
    assert q["references"] == ["<a@x> <b@y>"]


def test_empty_body_skips_body_param() -> None:
    url = build_mailto("a@example.com", subject="hi", body="")
    _, q = _split(url)
    assert "body" not in q


def test_emoji_subject_round_trips() -> None:
    url = build_mailto("a@example.com", subject="🚀 launch")
    _, q = _split(url)
    assert q["subject"] == ["🚀 launch"]


def test_plus_in_local_part_stays_literal() -> None:
    url = build_mailto("user+tag@example.com")
    assert "user+tag@example.com" in url


def test_no_query_omits_question_mark() -> None:
    url = build_mailto("a@example.com")
    assert url == "mailto:a@example.com"


def test_iso_link_detector_friendly_addr_chars() -> None:
    url = build_mailto("first.last_x-y@sub.mail.example.org")
    assert "first.last_x-y@sub.mail.example.org" in url


def test_build_mailto_raises_over_length_limit() -> None:
    from src.brain.mcp.tools._mailto import _MAX_MAILTO_URL

    build_mailto(to="a@example.com", body="a short body")
    try:
        build_mailto(to="a@example.com", body="x" * (_MAX_MAILTO_URL + 500))
    except ValueError as e:
        assert "silently truncates" in str(e)
    else:
        raise AssertionError("over-length mailto URL did not raise")
