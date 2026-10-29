from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import vobject

from src.brain.actions.handlers.update_calendar_event import (
    _apply_patch,
    _coerce,
    execute,
)


def test_coerce_existing_date_preserves_date_type() -> None:
    out = _coerce("2026-05-12", existing=date(2026, 1, 1))
    assert isinstance(out, date) and not isinstance(out, datetime)


def test_coerce_existing_datetime_preserves_datetime() -> None:
    out = _coerce("2026-05-12T14:00:00Z", existing=datetime(2026, 1, 1, 12, 0))
    assert isinstance(out, datetime)


def test_coerce_z_suffix_becomes_utc_datetime() -> None:
    out = _coerce("2026-05-12T14:00:00Z", existing=datetime(2026, 1, 1, 12, 0))
    assert out.utcoffset().total_seconds() == 0


def test_all_day_absolute_alarm_moves_with_new_start_date() -> None:
    cal = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:u\r\nSUMMARY:old\r\nDTSTART;VALUE=DATE:20260512\r\n"
        "DTEND;VALUE=DATE:20260513\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:old\r\nTRIGGER;VALUE=DATE-TIME:20260512T070000Z\r\n"
        "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    _apply_patch(cal.vevent, {"start": "2026-05-19"})

    assert cal.vevent.valarm.trigger.value.astimezone(UTC) == datetime(
        2026, 5, 19, 7, tzinfo=UTC
    )


def test_relative_alarm_is_unchanged_when_start_moves() -> None:
    cal = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:u\r\nSUMMARY:old\r\nDTSTART;VALUE=DATE:20260512\r\n"
        "DTEND;VALUE=DATE:20260513\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:old\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    _apply_patch(cal.vevent, {"start": "2026-05-19"})

    assert cal.vevent.valarm.trigger.value == timedelta(minutes=-15)


_ICLOUD_URL = "https://p15-caldav.icloud.com/u/calendars/home/event.ics"


def _mock_client() -> MagicMock:
    from caldav.lib.url import URL

    client = MagicMock()
    client.url = URL.objectify("https://p15-caldav.icloud.com/")
    return client


def test_execute_rejects_empty_patch() -> None:
    result = asyncio.run(execute({"event_url": _ICLOUD_URL}))
    assert result["ok"] is False
    assert "no fields to update" in result["result"]


def test_execute_rejects_missing_event_url() -> None:
    result = asyncio.run(execute({"summary": "x"}))
    assert result["ok"] is False
    assert "missing event_url" in result["result"]


def test_execute_rejects_non_icloud_url() -> None:
    result = asyncio.run(execute({"event_url": "https://evil.example/x", "summary": "x"}))
    assert result["ok"] is False
    assert "iCloud" in result["result"]


def _fake_412_error() -> Exception:
    try:
        from caldav.lib.error import PutError  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("caldav.lib.error.PutError not importable")
    err = PutError("412 Precondition Failed")
    err.reason = 412
    return err


def _fake_event_factory(save_side_effects):
    saves = iter(save_side_effects)

    def _save():
        next_eff = next(saves)
        if isinstance(next_eff, Exception):
            raise next_eff
        return next_eff

    fake = MagicMock()
    fake.load = MagicMock()
    fake.save = MagicMock(side_effect=lambda: _save())
    fake.data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:u\r\nSUMMARY:old\r\nDTSTART:20260101T120000Z\r\n"
        "DTEND:20260101T130000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return fake


def test_etag_412_retries_once_then_succeeds() -> None:
    fake = _fake_event_factory([_fake_412_error(), None])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))
    assert result["ok"] is True, result
    assert fake.load.call_count == 2
    assert fake.save.call_count == 2


def test_etag_412_twice_surfaces_failure() -> None:
    fake = _fake_event_factory([_fake_412_error(), _fake_412_error()])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))
    assert result["ok"] is False
    assert "412" in result["result"] or "ETag" in result["result"]


def test_etag_bare_exception_not_misclassified() -> None:
    fake = _fake_event_factory([Exception("412 Precondition Failed"), None])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))
    assert result["ok"] is False
    assert fake.save.call_count == 1


def test_lost_update_response_is_ambiguous_not_retryable() -> None:
    fake = _fake_event_factory([TimeoutError("response lost")])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True


def test_save_with_real_caldav_event_puts_to_the_same_url() -> None:
    # Regression: caldav 3.x save() resolves the event path against its parent
    # calendar's URL. An Event built from only client and url has no parent, and
    # save() raised AttributeError: 'NoneType' object has no attribute 'url'.
    client = _mock_client()
    client.request.return_value = MagicMock(
        status=200,
        headers={"ETag": '"e1"'},
        raw=(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:u\r\nSUMMARY:old\r\nDTSTART:20260101T120000Z\r\n"
            "DTEND:20260101T130000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
    )
    client.put.return_value = MagicMock(status=204, headers={})
    with patch("src.brain.mcp.tools._icloud.caldav_client", return_value=client):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))

    assert result["ok"] is True, result
    put_url, put_body = client.put.call_args.args[:2]
    assert str(put_url) == _ICLOUD_URL
    assert "SUMMARY:new" in put_body


def test_etag_mismatch_error_retries_once_then_succeeds() -> None:
    # caldav 3.x raises ETagMismatchError (not a PutError) for a 412 on an
    # event loaded with an ETag; it must still count as a lost ETag race.
    from caldav.lib.error import ETagMismatchError

    fake = _fake_event_factory([ETagMismatchError("412 Precondition Failed"), None])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "summary": "new"}))
    assert result["ok"] is True, result
    assert fake.save.call_count == 2


def _two_alarm_event(start_line: str = "DTSTART:20260512T140000Z",
                     end_line: str = "DTEND:20260512T150000Z") -> vobject.base.Component:
    return vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:u\r\nSUMMARY:old\r\n{start_line}\r\n{end_line}\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:old\r\nTRIGGER:-PT15M\r\n"
        "END:VALARM\r\n"
        "BEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER:-P1D\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _alarm_blocks(ics: str) -> list[str]:
    text = ics.replace("\r\n", "\n")
    blocks = []
    while "BEGIN:VALARM" in text:
        head, _, rest = text.partition("BEGIN:VALARM")
        body, _, text = rest.partition("END:VALARM")
        blocks.append(body)
    return blocks


def test_update_replaces_alarm_not_duplicates() -> None:
    cal = _two_alarm_event()

    _apply_patch(cal.vevent, {"alert_minutes": 60})

    blocks = _alarm_blocks(cal.serialize())
    assert len(blocks) == 1
    assert "ACTION:DISPLAY" in blocks[0]
    assert "TRIGGER:-PT60M" in blocks[0]


def test_update_alert_zero_is_at_start() -> None:
    cal = _two_alarm_event()

    _apply_patch(cal.vevent, {"alert_minutes": 0})

    blocks = _alarm_blocks(cal.serialize())
    assert len(blocks) == 1
    assert "TRIGGER:-PT0M" in blocks[0]


def test_update_adds_alarm_when_event_has_none() -> None:
    cal = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:u\r\nSUMMARY:old\r\nDTSTART:20260512T140000Z\r\n"
        "DTEND:20260512T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    _apply_patch(cal.vevent, {"alert_minutes": 30})

    blocks = _alarm_blocks(cal.serialize())
    assert len(blocks) == 1
    assert "TRIGGER:-PT30M" in blocks[0]


def test_update_without_alert_minutes_leaves_alarms_alone() -> None:
    cal = _two_alarm_event()

    _apply_patch(cal.vevent, {"summary": "new"})
    _apply_patch(cal.vevent, {"summary": "newer", "alert_minutes": None})

    blocks = _alarm_blocks(cal.serialize())
    assert len(blocks) == 2
    assert "TRIGGER:-PT15M" in blocks[0]


def test_update_all_day_alert_is_local_morning_of_the_new_start() -> None:
    cal = _two_alarm_event("DTSTART;VALUE=DATE:20260512", "DTEND;VALUE=DATE:20260513")

    _apply_patch(cal.vevent, {"start": "2026-05-19", "alert_minutes": 60})

    assert len(cal.vevent.valarm_list) == 1
    assert cal.vevent.valarm.trigger.value.astimezone(UTC) == datetime(
        2026, 5, 19, 6, tzinfo=UTC
    )


@pytest.mark.parametrize("value", [True, -1, "5", 1.5, 10081])
def test_update_rejects_bad_alert_minutes_without_loading(value) -> None:
    with patch("src.brain.mcp.tools._icloud.caldav_client") as client:
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "alert_minutes": value}))

    assert result["ok"] is False
    assert "alert_minutes" in result["result"]
    client.assert_not_called()


def test_update_with_only_alert_minutes_is_a_real_update() -> None:
    fake = _fake_event_factory([None])
    with patch(
        "src.brain.mcp.tools._icloud.caldav_client", return_value=_mock_client(),
    ), patch("caldav.Event", return_value=fake):
        result = asyncio.run(execute({"event_url": _ICLOUD_URL, "alert_minutes": 45}))

    assert result["ok"] is True, result
    assert "TRIGGER:-PT45M" in fake.data


def test_update_with_only_none_alert_minutes_has_nothing_to_update() -> None:
    result = asyncio.run(execute({"event_url": _ICLOUD_URL, "alert_minutes": None}))

    assert result["ok"] is False
    assert "no fields to update" in result["result"]
