from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest
import vobject

from src.brain.actions.handlers.create_contact import (
    _build_vcard,
    _emit_typed_lines,
    _escape_vcard_value,
    execute,
)


def test_escape_backslash_is_doubled() -> None:
    assert _escape_vcard_value("a\\b") == "a\\\\b"


def test_escape_combined_does_not_double_backslash_the_escapes() -> None:
    assert _escape_vcard_value("a,b\\c") == "a\\,b\\\\c"


def test_build_vcard_round_trips_comma_in_name() -> None:
    text = _build_vcard({"full_name": "Doe, Jane", "family_name": "Doe", "given_name": "Jane"}, uid="UID-1")
    v = vobject.readOne(text)
    assert v.fn.value == "Doe, Jane"
    assert v.n.value.family == "Doe"
    assert v.n.value.given == "Jane"


def test_build_vcard_round_trips_semicolon_in_org() -> None:
    text = _build_vcard({"full_name": "Jane Doe", "org": "Acme; Inc."}, uid="UID-2")
    v = vobject.readOne(text)
    org_val = v.org.value
    org_str = org_val[0] if isinstance(org_val, list) else org_val
    assert org_str == "Acme; Inc."


def test_build_vcard_round_trips_newline_in_note() -> None:
    text = _build_vcard({"full_name": "Jane Doe", "note": "line1\nline2"}, uid="UID-3")
    v = vobject.readOne(text)
    assert v.note.value == "line1\nline2"


def test_build_vcard_has_required_fields() -> None:
    text = _build_vcard({"full_name": "Jane Doe"}, uid="UID-4")
    assert "BEGIN:VCARD" in text
    assert "VERSION:3.0" in text
    assert "UID:UID-4" in text
    assert "END:VCARD" in text


def test_emit_typed_lines_string_form() -> None:
    out = _emit_typed_lines("EMAIL", ["jane@example.com", "jane@example.org"])
    assert out == ["EMAIL:jane@example.com", "EMAIL:jane@example.org"]


def test_emit_typed_lines_dict_with_single_type() -> None:
    out = _emit_typed_lines("EMAIL", [{"value": "jane@example.com", "type": "WORK"}])
    assert out == ["EMAIL;TYPE=WORK:jane@example.com"]


def test_emit_typed_lines_dict_with_multi_type() -> None:
    out = _emit_typed_lines("EMAIL", [{"value": "jane@example.com", "type": ["WORK", "PREF"]}])
    assert out == ["EMAIL;TYPE=WORK,TYPE=PREF:jane@example.com"]


def test_emit_typed_lines_empty_value_skipped() -> None:
    out = _emit_typed_lines("EMAIL", [{"value": "", "type": "WORK"}, "jane@example.com"])
    assert out == ["EMAIL:jane@example.com"]


def test_emit_typed_lines_escapes_value() -> None:
    out = _emit_typed_lines("EMAIL", ["weird,addr@example.com"])
    assert out == ["EMAIL:weird\\,addr@example.com"]


def test_emit_typed_lines_rejects_parameter_injection() -> None:
    with pytest.raises(ValueError, match="TYPE token"):
        _emit_typed_lines(
            "EMAIL",
            [{"value": "jane@example.com", "type": "HOME:\r\nNOTE:injected"}],
        )


def _carddav_client(status_code: int):
    client = MagicMock()
    response = MagicMock(status_code=status_code, text="remote response")
    client.put.return_value = response

    @contextlib.contextmanager
    def factory():
        yield client

    return factory


def test_carddav_503_is_retryable() -> None:
    with patch(
        "src.brain.mcp.tools._icloud.carddav_http_client",
        _carddav_client(503),
    ), patch(
        "src.brain.mcp.tools._icloud.carddav_addressbook_url",
        return_value="https://contacts.icloud.com/card/",
    ):
        result = asyncio.run(execute({"full_name": "Jane Doe", "_action_id": "a1"}))

    assert result["ok"] is False
    assert result["retryable"] is True


def test_carddav_412_is_idempotent_for_queue_retry() -> None:
    with patch(
        "src.brain.mcp.tools._icloud.carddav_http_client",
        _carddav_client(412),
    ), patch(
        "src.brain.mcp.tools._icloud.carddav_addressbook_url",
        return_value="https://contacts.icloud.com/card/",
    ):
        result = asyncio.run(execute({"full_name": "Jane Doe", "_action_id": "a1"}))

    assert result["ok"] is True
    assert "idempotent retry" in result["result"]
