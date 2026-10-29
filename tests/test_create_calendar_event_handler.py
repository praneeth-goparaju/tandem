from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import vobject

from src.brain.actions.handlers.create_calendar_event import (
    _build_vevent,
    _parse_dt,
    _pick_calendar,
    execute,
)


def test_parse_dt_z_suffix_becomes_utc() -> None:
    dt = _parse_dt("2026-05-12T14:00:00Z", all_day=False)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_dt_naive_assumed_utc() -> None:
    dt = _parse_dt("2026-05-12T14:00:00", all_day=False)
    assert isinstance(dt, datetime)
    assert dt.utcoffset() == timedelta(0)


def test_parse_dt_aware_offset_normalized_to_utc() -> None:
    # Offset-only tzinfo (e.g. timezone(timedelta(hours=2))) breaks vobject's
    # pickTzid, so we astimezone-to-UTC. Wall-clock display in iCloud is the
    # same — Apple renders DTSTART in the user's local TZ either way.
    dt = _parse_dt("2026-05-12T14:00:00+02:00", all_day=False)
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 12


def test_parse_dt_all_day_returns_date() -> None:
    d = _parse_dt("2026-05-12", all_day=True)
    assert isinstance(d, date) and not isinstance(d, datetime)


def test_parse_dt_malformed_raises() -> None:
    with pytest.raises(ValueError):
        _parse_dt("not a date", all_day=False)


def _payload(**overrides):
    base = {
        "summary": "Meeting",
        "start": "2026-05-12T14:00:00+02:00",
        "end": "2026-05-12T15:00:00+02:00",
    }
    base.update(overrides)
    return base


def test_build_vevent_round_trips_uid_and_summary() -> None:
    ics = _build_vevent(_payload(), uid="my-uid")
    cal = vobject.readOne(ics)
    assert cal.vevent.uid.value == "my-uid"
    assert cal.vevent.summary.value == "Meeting"


def test_build_vevent_includes_dtstamp() -> None:
    ics = _build_vevent(_payload(), uid="x")
    cal = vobject.readOne(ics)
    assert hasattr(cal.vevent, "dtstamp")
    assert isinstance(cal.vevent.dtstamp.value, datetime)


def test_build_vevent_attendee_has_mailto_prefix_and_rsvp() -> None:
    ics = _build_vevent(_payload(attendees=["alice@example.com"]), uid="x")
    cal = vobject.readOne(ics)
    att = cal.vevent.attendee
    assert att.value.startswith("mailto:")
    assert att.value == "mailto:alice@example.com"
    assert att.params.get("RSVP") == ["TRUE"]


def test_build_vevent_all_day_uses_date_not_datetime() -> None:
    ics = _build_vevent(
        _payload(all_day=True, start="2026-05-12", end="2026-05-13"),
        uid="x",
    )
    cal = vobject.readOne(ics)
    assert isinstance(cal.vevent.dtstart.value, date)
    assert not isinstance(cal.vevent.dtstart.value, datetime)


def test_build_vevent_optional_fields_omitted_when_absent() -> None:
    ics = _build_vevent(_payload(), uid="x")
    cal = vobject.readOne(ics)
    assert not hasattr(cal.vevent, "location")
    assert not hasattr(cal.vevent, "description")


class _StubCal:
    def __init__(self, name: str) -> None:
        self.name = name


class _ModernStubCal:
    def __init__(self, display_name: str) -> None:
        self._display_name = display_name

    def get_display_name(self) -> str:
        return self._display_name

    @property
    def name(self) -> str:
        raise AssertionError("deprecated .name property should not be read")


class _StubPrincipal:
    def __init__(self, cals):
        self._cals = cals

    def calendars(self):
        return self._cals


class _StubClient:
    def __init__(self, cals):
        self._principal = _StubPrincipal(cals)

    def principal(self):
        return self._principal


def test_pick_calendar_no_filter_prefers_icloud_over_first():
    cals = [_StubCal("Home"), _StubCal("Calendar"), _StubCal("iCloud"), _StubCal("Family")]
    assert _pick_calendar(_StubClient(cals), "") is cals[2]


def test_pick_calendar_no_filter_prefers_calendar_when_no_icloud():
    cals = [_StubCal("Home"), _StubCal("Calendar"), _StubCal("Family")]
    assert _pick_calendar(_StubClient(cals), "") is cals[1]


def test_pick_calendar_no_filter_falls_back_to_first_when_no_preferred_match():
    cals = [_StubCal("Personal"), _StubCal("Work")]
    assert _pick_calendar(_StubClient(cals), "") is cals[0]


def test_pick_calendar_explicit_name_filter_substring_match_still_works():
    cals = [_StubCal("Home"), _StubCal("Calendar"), _StubCal("Family")]
    assert _pick_calendar(_StubClient(cals), "fam") is cals[2]


def test_pick_calendar_returns_none_when_no_calendars():
    assert _pick_calendar(_StubClient([]), "") is None


def test_pick_calendar_returns_none_when_filter_doesnt_match():
    cals = [_StubCal("Home"), _StubCal("Calendar")]
    assert _pick_calendar(_StubClient(cals), "nonexistent") is None


def test_pick_calendar_calendar_match_is_case_insensitive():
    cals = [_StubCal("Home"), _StubCal("CALENDAR")]
    assert _pick_calendar(_StubClient(cals), "") is cals[1]


def test_pick_calendar_icloud_match_is_case_insensitive():
    cals = [_StubCal("Home"), _StubCal("ICLOUD"), _StubCal("Calendar")]
    assert _pick_calendar(_StubClient(cals), "") is cals[1]


def test_pick_calendar_uses_modern_display_name_api():
    cals = [_ModernStubCal("Home"), _ModernStubCal("iCloud")]
    assert _pick_calendar(_StubClient(cals), "") is cals[1]


def test_lost_caldav_save_response_is_ambiguous_not_retryable() -> None:
    calendar = _StubCal("iCloud")
    calendar.save_event = MagicMock(side_effect=TimeoutError("slow"))
    client = _StubClient([calendar])

    with patch("src.brain.mcp.tools._icloud.caldav_client", return_value=client):
        result = asyncio.run(execute({
            "summary": "Meeting",
            "start": "2026-05-12T14:00:00Z",
            "end": "2026-05-12T15:00:00Z",
            "_action_id": "action-1",
        }))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True


def test_build_vevent_alarm_minutes_before_zero_is_at_start() -> None:
    cal = vobject.readOne(_build_vevent(_payload(alarm_minutes_before=0), uid="x"))

    alarm = cal.vevent.valarm
    assert alarm.action.value == "DISPLAY"
    assert alarm.trigger.value == timedelta(0)


def test_build_vevent_adds_custom_relative_alarm() -> None:
    cal = vobject.readOne(_build_vevent(
        _payload(alarm_minutes_before=15), uid="x",
    ))

    assert cal.vevent.valarm.trigger.value == timedelta(minutes=-15)


def test_build_vevent_all_day_alarm_is_local_morning() -> None:
    cal = vobject.readOne(_build_vevent(
        _payload(
            all_day=True,
            start="2026-05-12",
            end="2026-05-13",
            alarm_minutes_before=0,
        ),
        uid="x",
    ))

    trigger = cal.vevent.valarm.trigger.value
    assert trigger.astimezone(UTC) == datetime(2026, 5, 12, 7, tzinfo=UTC)


@pytest.mark.parametrize("value", [True, -1, 10081, "5"])
def test_build_vevent_rejects_invalid_alarm_offset(value) -> None:
    with pytest.raises(ValueError, match="alarm_minutes_before"):
        _build_vevent(_payload(alarm_minutes_before=value), uid="x")


def _alarm_block(ics: str) -> str:
    lines = ics.replace("\r\n", "\n").split("\n")
    start = lines.index("BEGIN:VALARM")
    end = lines.index("END:VALARM")
    return "\n".join(lines[start : end + 1])


def test_default_alert_60_minutes() -> None:
    ics = _build_vevent(_payload(), uid="x")

    assert ics.count("BEGIN:VALARM") == 1
    block = _alarm_block(ics)
    assert "ACTION:DISPLAY" in block
    assert "TRIGGER:-PT60M" in block


def test_none_alert_minutes_means_default_60() -> None:
    ics = _build_vevent(_payload(alert_minutes=None), uid="x")

    assert "TRIGGER:-PT60M" in _alarm_block(ics)


def test_alert_zero_at_start() -> None:
    ics = _build_vevent(_payload(alert_minutes=0), uid="x")

    assert ics.count("BEGIN:VALARM") == 1
    assert "TRIGGER:-PT0M" in _alarm_block(ics)


def test_alert_custom_minutes() -> None:
    ics = _build_vevent(_payload(alert_minutes=15), uid="x")

    assert "TRIGGER:-PT15M" in _alarm_block(ics)


def test_alert_minutes_wins_over_alarm_minutes_before() -> None:
    ics = _build_vevent(_payload(alert_minutes=10, alarm_minutes_before=30), uid="x")

    assert "TRIGGER:-PT10M" in _alarm_block(ics)


def test_all_day_default_alert_is_an_hour_before_local_morning() -> None:
    cal = vobject.readOne(_build_vevent(
        _payload(all_day=True, start="2026-05-12", end="2026-05-13"), uid="x",
    ))

    trigger = cal.vevent.valarm.trigger.value
    assert trigger.astimezone(UTC) == datetime(2026, 5, 12, 6, tzinfo=UTC)


@pytest.mark.parametrize("value", [True, -1, "5", 1.5, 10081])
def test_build_vevent_rejects_invalid_alert_minutes(value) -> None:
    with pytest.raises(ValueError, match="alert_minutes"):
        _build_vevent(_payload(alert_minutes=value), uid="x")


def test_execute_reports_bad_alert_minutes_without_saving() -> None:
    calendar = _StubCal("iCloud")
    calendar.save_event = MagicMock()
    client = _StubClient([calendar])

    with patch("src.brain.mcp.tools._icloud.caldav_client", return_value=client):
        result = asyncio.run(execute({
            "summary": "Meeting",
            "start": "2026-05-12T14:00:00Z",
            "end": "2026-05-12T15:00:00Z",
            "alert_minutes": -5,
        }))

    assert result["ok"] is False
    assert "alert_minutes" in result["result"]
    calendar.save_event.assert_not_called()
