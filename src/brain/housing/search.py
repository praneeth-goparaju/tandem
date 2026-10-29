from __future__ import annotations

import re

from . import require_configured
from .models import TARGET_CITIES, TARGET_POSTCODES, Listing, SearchCriteria
from .scrapers import search_all_reporting
from .storage import (
    get_db,
    list_watches,
    record_watch_hits,
    upsert_listings,
)

_TITLE_FILLER = frozenset({
    "charmantes", "charmante", "schönes", "schöne", "modernes", "moderne",
    "traumhaftes", "traumhafte", "gepflegtes", "gepflegte", "exklusives",
    "exklusive", "wunderschönes", "wunderschöne", "einzigartiges", "einzigartige",
    "großzügiges", "großzügige", "helles", "helle", "top", "neuwertiges",
    "neuwertige", "gemütliches", "gemütliche", "attraktives", "attraktive",
    "hochwertiges", "hochwertige", "repräsentatives", "repräsentative",
    "freistehendes", "freistehende", "saniertes", "sanierte", "renoviertes",
    "renovierte", "modernisiertes", "modernisierte",
    "großes", "großer", "großen", "großem", "großartiges", "kleines",
    "kleiner", "kleinen", "kleinem", "riesiges", "weitläufiges", "geräumiges",
    "einfamilienhaus", "efh", "haus", "villa", "bungalow", "reihenhaus",
    "doppelhaushälfte", "dhh", "zweifamilienhaus", "zfh", "mehrfamilienhaus",
    "mfh", "stadthaus",
    "in", "mit", "bei", "am", "an", "der", "die", "das", "ein", "eine",
    "einen", "und", "für", "von", "zu", "im", "auf", "aus", "nach", "zum",
    "zur", "vom", "beim",
})

_WORD_RE = re.compile(r"[a-zäöüß]+", re.IGNORECASE)


def _title_signature(title: str, n: int = 3) -> str:
    words = [w.lower() for w in _WORD_RE.findall(title or "")]
    meaningful = [w for w in words if w not in _TITLE_FILLER]
    return " ".join(meaningful[:n])


PRICE_TOLERANCE_EUR = 10_000


def _is_same_property(a: Listing, b: Listing) -> bool:
    if not (a.price_eur and b.price_eur):
        return False
    if abs(a.price_eur - b.price_eur) > PRICE_TOLERANCE_EUR:
        return False
    if a.postcode and b.postcode and a.postcode != b.postcode:
        return False
    sig_a = _title_signature(a.title)
    sig_b = _title_signature(b.title)
    return bool(sig_a) and sig_a == sig_b


def _dedup(listings: list[Listing]) -> list[Listing]:
    kept: list[Listing] = []
    for lst in listings:
        if any(_is_same_property(lst, k) for k in kept):
            continue
        kept.append(lst)
    return kept


def _in_corridor(lst: Listing) -> bool:
    if lst.postcode and lst.postcode in TARGET_POSTCODES:
        return True
    city = (lst.city or "").lower()
    if any(target in city for target in TARGET_CITIES):
        return True
    addr = (lst.address or "").lower()
    return any(target in addr for target in TARGET_CITIES)


async def run_search_reporting(
    criteria: SearchCriteria,
    sources: list[str] | None = None,
    strict_corridor: bool = True,
) -> tuple[list[Listing], dict[str, str]]:
    """Run a one-shot search, persist, and return the deduped list and the sources that failed."""
    require_configured()
    raw, failures = await search_all_reporting(criteria, enabled=sources)
    if strict_corridor:
        raw = [lst for lst in raw if _in_corridor(lst)]
    deduped = _dedup(raw)

    conn = get_db()
    upsert_listings(conn, deduped)
    return deduped, failures


async def run_search(
    criteria: SearchCriteria,
    sources: list[str] | None = None,
    strict_corridor: bool = True,
) -> list[Listing]:
    return (await run_search_reporting(criteria, sources, strict_corridor))[0]


async def run_watches() -> dict[str, list[Listing]]:
    require_configured()
    conn = get_db()
    watches = list_watches(conn)
    results: dict[str, list[Listing]] = {}

    for w in watches:
        c = SearchCriteria(**w["criteria"])
        listings = await run_search(c)
        new_ids = record_watch_hits(conn, w["name"], [lst.id for lst in listings])
        new_listings = [lst for lst in listings if lst.id in set(new_ids)]
        results[w["name"]] = new_listings

    return results
