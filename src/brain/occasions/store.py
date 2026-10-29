from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

from src.brain.atomic_io import atomic_write_text, read_regular_text
from src.brain.occasions.models import (
    FestivalsFile,
    PeopleFile,
    Person,
    Tier,
    VoiceFile,
)
from src.orchestrator import config

DOSSIER_DIR = "12 - People"


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(read_regular_text(path)) or {}
    except Exception:  # noqa: BLE001
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, body)


def load_people() -> PeopleFile:
    return PeopleFile.model_validate(_load_yaml(config.OCCASIONS_PEOPLE))


def save_people(people: PeopleFile) -> None:
    _atomic_write(config.OCCASIONS_PEOPLE, people.model_dump(mode="json"))


def load_festivals() -> FestivalsFile:
    return FestivalsFile.model_validate(_load_yaml(config.OCCASIONS_FESTIVALS))


def load_voice() -> VoiceFile:
    return VoiceFile.model_validate(_load_yaml(config.OCCASIONS_VOICE))


def save_voice(voice: VoiceFile) -> None:
    _atomic_write(config.OCCASIONS_VOICE, voice.model_dump(mode="json"))


def people_by_id() -> dict[str, Person]:
    return {p.id: p for p in load_people().people}


def local_date(instant: datetime | None = None) -> date:
    value = instant or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(config.TIMEZONE)).date()


def reminder_identity_status(
    person_id: str,
    occasion: str,
    year: int,
    *,
    created_on: date,
    executing_on: date,
) -> Literal["due", "already_reminded", "invalid", "stale"]:
    from src.brain.occasions.occasion_guard import window_hits

    person = people_by_id().get(person_id)
    if person is None:
        return "invalid"
    festivals = load_festivals().festivals
    hit = next((
        item
        for item in window_hits([person], festivals, created_on, days=1)
        if item["person_id"] == person_id
        and item["occasion"] == occasion
        and int(item["date"][:4]) == year
    ), None)
    if hit is None:
        return "invalid"
    if occasion_key(occasion, year) in person.last_reminded:
        return "already_reminded"
    return "stale" if date.fromisoformat(hit["date"]) < executing_on else "due"


def dossier_path(person: Person) -> str:
    if person.dossier:
        return person.dossier
    safe = (person.name or person.id or "person").replace("/", "-").replace("\\", "-").strip()
    return f"{DOSSIER_DIR}/{safe}.md"


def occasion_key(occasion: str, year: int) -> str:
    return f"{occasion}:{year}"


def already_reminded(person_id: str, occasion: str, year: int) -> bool:
    p = people_by_id().get(person_id)
    return bool(p and occasion_key(occasion, year) in p.last_reminded)


def stamp_reminded(person_id: str, occasion: str, year: int, ts: str | None = None) -> bool:
    """Record a Slack reminder so the scheduled pass does not repeat it.

    Returns ``False`` when the person no longer exists, allowing the tool layer
    to surface stale ids instead of claiming a reminder was recorded.
    """
    pf = load_people()
    for p in pf.people:
        if p.id == person_id:
            p.last_reminded[occasion_key(occasion, year)] = ts or datetime.now(UTC).isoformat()
            save_people(pf)
            return True
    return False


TIER_CADENCE = {"inner": "monthly", "close": "quarterly", "extended": None}
CADENCE_DAYS = {
    "weekly": 7, "fortnightly": 14, "monthly": 30,
    "quarterly": 91, "biannual": 182, "yearly": 365,
}


def effective_cadence(person: Person) -> str | None:
    if person.cadence is not None:
        return person.cadence or None
    return TIER_CADENCE.get(person.tier)


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "person"


def add_person(name: str, *, e164: str | None = None, tier: Tier = "close",
               in_scope: bool = False) -> Person:
    pf = load_people()
    ids = {p.id for p in pf.people}
    base = _slugify(name)
    pid, n = base, 2
    while pid in ids:
        pid = f"{base}-{n}"
        n += 1
    person = Person(id=pid, name=name, e164=e164, tier=tier, in_scope=in_scope)
    pf.people.append(person)
    save_people(pf)
    return person


def update_person(person_id: str, **fields) -> Person | None:
    pf = load_people()
    for p in pf.people:
        if p.id == person_id:
            for k, v in fields.items():
                if v is not None and hasattr(p, k):
                    setattr(p, k, v)
            save_people(pf)
            return p
    return None


def set_occasion(person_id: str, occasion: str, *, date: str | None = None,
                 enabled: bool | None = None) -> Person | None:
    """Set an occasion for a person. 'birthday'/'anniversary' set date + enabled
    (a date implies enabled); any other value is treated as a festival id to add
    (enabled=False removes it). Returns the person, or None if not found."""
    pf = load_people()
    for p in pf.people:
        if p.id != person_id:
            continue
        if occasion in ("birthday", "anniversary"):
            flag = getattr(p.occasions, occasion)
            if date is not None:
                flag.date = date or None
            if enabled is not None:
                flag.enabled = enabled
            elif date:
                flag.enabled = True
        else:
            fests = set(p.occasions.festivals)
            fests.discard(occasion) if enabled is False else fests.add(occasion)
            p.occasions.festivals = sorted(fests)
        save_people(pf)
        return p
    return None


def bump_last_interaction(person_id: str, when: str | None = None) -> None:
    pf = load_people()
    for p in pf.people:
        if p.id == person_id:
            p.last_interaction = when or datetime.now(UTC).date().isoformat()
            save_people(pf)
            return


def stay_in_touch_due(today: date) -> list[dict]:
    """People overdue for contact: cadence set, a last_interaction baseline
    exists, and more than the cadence window has passed. People with no logged
    interaction yet are NOT nudged (no baseline → not 'going cold'), which keeps
    a freshly-imported roster from flooding you on day one. Soonest-overdue last."""
    out: list[dict] = []
    for p in load_people().people:
        cad = effective_cadence(p)
        if not cad or cad not in CADENCE_DAYS or not p.last_interaction:
            continue
        try:
            last = date.fromisoformat(p.last_interaction)
        except ValueError:
            continue
        days = (today - last).days
        if days > CADENCE_DAYS[cad]:
            out.append({
                "person_id": p.id, "name": p.name, "tier": p.tier,
                "cadence": cad, "last_interaction": p.last_interaction, "days_since": days,
            })
    return sorted(out, key=lambda x: -x["days_since"])
