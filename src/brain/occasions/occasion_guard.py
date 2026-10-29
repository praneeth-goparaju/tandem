from __future__ import annotations

import re
from datetime import date, timedelta

_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YEARLESS = re.compile(r"^--(\d{2})-(\d{2})$")


def parse_occasion_date(s: str | None) -> tuple[int | None, int, int] | None:
    if not s:
        return None
    s = str(s).strip()
    m = _YEARLESS.match(s)
    if m:
        return None, int(m.group(1)), int(m.group(2))
    m = _FULL.match(s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise


def next_occurrence(s: str | None, today: date) -> date | None:
    parsed = parse_occasion_date(s)
    if not parsed:
        return None
    _year, month, day = parsed
    occ = _safe_date(today.year, month, day)
    if occ < today:
        occ = _safe_date(today.year + 1, month, day)
    return occ


def _festival_next(fest, today: date) -> date | None:
    upcoming: list[date] = []
    for ds in (getattr(fest, "dates", None) or {}).values():
        try:
            dd = date.fromisoformat(str(ds))
        except (TypeError, ValueError):
            continue
        if dd >= today:
            upcoming.append(dd)
    return min(upcoming) if upcoming else None


def window_hits(people, festivals, today: date, days: int) -> list[dict]:
    """All occasions for in-scope people falling within [today, today+days].

    `people` is an iterable of Person, `festivals` a dict[str, Festival].
    Returns dicts {person_id, person_name, occasion, date, days_out}, soonest
    first. `occasion` is 'birthday' / 'anniversary' / a festival id.
    """
    horizon = today + timedelta(days=days)
    hits: list[dict] = []

    def _add(person, occasion: str, occ: date | None) -> None:
        if occ and today <= occ <= horizon:
            hits.append(
                {
                    "person_id": person.id,
                    "person_name": person.name,
                    "occasion": occasion,
                    "date": occ.isoformat(),
                    "days_out": (occ - today).days,
                }
            )

    for p in people:
        if not getattr(p, "in_scope", True):
            continue
        occ = p.occasions
        if occ.birthday.enabled and occ.birthday.date:
            _add(p, "birthday", next_occurrence(occ.birthday.date, today))
        if occ.anniversary.enabled and occ.anniversary.date:
            _add(p, "anniversary", next_occurrence(occ.anniversary.date, today))
        for fid in occ.festivals:
            fest = festivals.get(fid)
            if fest is not None:
                _add(p, fid, _festival_next(fest, today))

    return sorted(hits, key=lambda h: (h["days_out"], h["person_name"]))
