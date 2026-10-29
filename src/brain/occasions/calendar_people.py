from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta

from src.brain.occasions import store

_log = logging.getLogger(__name__)


def _parse_event(ev) -> tuple[str, str, str]:
    import vobject

    try:
        cal = ev.icalendar_instance if hasattr(ev, "icalendar_instance") else vobject.readOne(ev.data)
    except Exception:  # noqa: BLE001
        return "", "", ""
    for ve in getattr(cal, "vevent_list", []):
        summ = getattr(ve, "summary", None)
        s = getattr(ve, "dtstart", None)
        e = getattr(ve, "dtend", None)
        return (
            (summ.value if summ else ""),
            (str(s.value) if s else ""),
            (str(e.value) if e else ""),
        )
    return "", "", ""


def _fetch_events(d_from: date, d_to: date) -> list[dict]:
    from src.brain.mcp.tools._icloud import ICloudNotConfigured, caldav_client

    try:
        cals = caldav_client().principal().calendars()
    except ICloudNotConfigured:
        return []
    except Exception as e:  # noqa: BLE001
        _log.warning("calendar fetch failed: %s: %s", type(e).__name__, e)
        return []

    start = datetime.combine(d_from, time.min, tzinfo=UTC)
    end = datetime.combine(d_to, time.min, tzinfo=UTC)
    out: list[dict] = []
    for cal in cals:
        try:
            evs = cal.search(start=start, end=end, event=True, expand=True)
        except Exception:  # noqa: BLE001
            continue
        for ev in evs:
            summary, start_value, end_value = _parse_event(ev)
            if summary:
                out.append({"summary": summary, "start": start_value, "end": end_value})
    return out


def _first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


def match_people_in_events(events: list[dict], people) -> list[dict]:
    patterns = []
    for p in people:
        names: set[str] = set()
        if p.name:
            names.add(p.name.lower())
        fn = _first_name(p.name).lower()
        if len(fn) >= 3:
            names.add(fn)
        if names:
            patterns.append((p, names))

    out: list[dict] = []
    for ev in events:
        title = (ev.get("summary") or "").lower()
        matched = [
            {"person_id": p.id, "name": p.name}
            for p, names in patterns
            if any(re.search(rf"\b{re.escape(n)}\b", title) for n in names)
        ]
        if matched:
            out.append({**ev, "matched": matched})
    return out


def meetings(scope: str = "today") -> list[dict]:
    today = datetime.now(UTC).date()
    d = {"tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1)}.get(scope, today)
    events = _fetch_events(d, d + timedelta(days=1))
    return match_people_in_events(events, store.load_people().people)
