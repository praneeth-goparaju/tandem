"""Kleinanzeigen (ex eBay-Kleinanzeigen) — houses for sale.

URL pattern used:
  https://www.kleinanzeigen.de/s-haus-kaufen/{city-slug}/c208

Each search page embeds ad cards as <article data-adid="...">. Until October
2026 they also had the class `aditem` and named sub-elements; the newer cards
use utility classes, so they are read by structure: the heading link, the
"<postcode> <city>" span, the "m² · Zi." line and the "€" line. A page with ad
links but no card either layout explains is an error, never "no listings".
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from src.orchestrator.config import HOUSING_CORRIDOR as _HC

from ..models import Listing, SearchCriteria
from . import _common

log = logging.getLogger(__name__)

BASE = "https://www.kleinanzeigen.de"
# category 208 = "Häuser zum Kauf"
CATEGORY_PATH = "s-haus-kaufen"

CORRIDOR_LOCATION_ID = _HC.kleinanzeigen_location_id if _HC.configured else ""
CORRIDOR_RADIUS_KM = _HC.kleinanzeigen_radius_km if _HC.configured else 0

_WOHNFL_SQM_RE = re.compile(
    r"(\d{2,4}(?:[.,]\d+)?)\s*(?:m²|qm)\s*(?:Wohnfläche|Wfl)",
    re.IGNORECASE,
)
_ANY_SQM_RE = re.compile(r"(?:ca\.?\s*)?(\d{2,4}(?:[.,]\d+)?)\s*(?:m²|qm)", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d)?)\s*(?:Zimmer|Zi\.?)\b", re.IGNORECASE)


def _extract_from_jsonld(article) -> tuple[float | None, float | None]:
    """Pull (rooms, sqm) out of Kleinanzeigen's embedded JSON-LD description.

    Search-result cards don't render rooms/sqm as DOM tags — but the listing's
    description blob lives in a `<script type="application/ld+json">` tag with
    free text like 'ca. 140 m² Wohnfläche' and '5 Zimmer'. Regex it out.
    """
    script = article.select_one('script[type="application/ld+json"]')
    if not script or not script.string:
        return None, None
    try:
        data = json.loads(script.string)
    except (json.JSONDecodeError, ValueError):
        return None, None
    desc = (data.get("description") or "") + " " + (data.get("title") or "")
    sqm = None
    m = _WOHNFL_SQM_RE.search(desc)
    if m:
        sqm = float(m.group(1).replace(",", "."))
    else:
        for cand in _ANY_SQM_RE.finditer(desc):
            v = float(cand.group(1).replace(",", "."))
            if 50 <= v <= 500:
                sqm = v
                break
    rooms = None
    m = _ROOMS_RE.search(desc)
    if m:
        rooms = float(m.group(1).replace(",", "."))
    return rooms, sqm


_POSTCODE_CITY_RE = re.compile(r"^\d{5}\s+\S")
_CARD_FACTS_RE = re.compile(r"m²|Zi\.", re.IGNORECASE)


def _extract_from_card(article) -> tuple[float | None, float | None]:
    for p in article.select("p"):
        text = p.get_text(" ", strip=True)
        if len(text) > 40 or not _CARD_FACTS_RE.search(text):
            continue
        sqm_match, rooms_match = _ANY_SQM_RE.search(text), _ROOMS_RE.search(text)
        sqm = float(sqm_match.group(1).replace(",", ".")) if sqm_match else None
        rooms = float(rooms_match.group(1).replace(",", ".")) if rooms_match else None
        return rooms, sqm
    return None, None


def _extract_city(loc_text: str, postcode: str) -> str:
    if not loc_text:
        return ""
    import re as _re
    t = _re.sub(r"\s+", " ", loc_text).strip()
    t = _re.sub(r"\([^)]*\)", "", t).strip()
    t = t.replace(postcode, "", 1).strip(" ,·-")
    return t.split(",")[0].strip().lower()


_CITY_SLUGS = _HC.kleinanzeigen_city_slugs if _HC.configured else {}

_DEFAULT_SLUG = next(iter(_CITY_SLUGS.values())) if _CITY_SLUGS else ""


async def search(criteria: SearchCriteria) -> list[Listing]:
    slug = _CITY_SLUGS.get(criteria.area.strip().lower(), _DEFAULT_SLUG)
    url = f"{BASE}/{CATEGORY_PATH}/{slug}/c208{CORRIDOR_LOCATION_ID}r{CORRIDOR_RADIUS_KM}"

    async with _common.client() as c:
        resp = await c.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    ads = soup.select("article[data-adid]")
    if not ads and "/s-anzeige/" in html:
        raise RuntimeError("page layout not recognised")
    out: list[Listing] = []
    for ad in ads:
        adid = ad.get("data-adid") or ""
        href = ad.select_one("a.ellipsis") or ad.select_one("h3 a[href]")
        title = href.get_text(strip=True) if href else ""
        href_value = (href.get("href") if href else None) or ad.get("data-href")
        link = BASE + str(href_value) if href_value else ""

        price_el = ad.select_one(".aditem-main--middle--price-shipping--price") or next(
            (p for p in ad.select("p") if "€" in p.get_text()), None
        )
        price = _common.parse_price_eur(price_el.get_text() if price_el else None)

        loc_el = ad.select_one(".aditem-main--top--left") or next(
            (span.parent for span in ad.select("span") if _POSTCODE_CITY_RE.match(span.get_text(strip=True))),
            None,
        )
        loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        postcode = _common.extract_postcode(loc_text)

        rooms, sqm = _extract_from_card(ad)
        if rooms is None or sqm is None:
            ld_rooms, ld_sqm = _extract_from_jsonld(ad)
            rooms = rooms if rooms is not None else ld_rooms
            sqm = sqm if sqm is not None else ld_sqm

        if not adid or not title:
            continue

        lst = Listing(
            source="kleinanzeigen",
            source_id=str(adid),
            url=link,
            title=title,
            price_eur=price,
            rooms=rooms,
            sqm=sqm,
            address=loc_text,
            postcode=postcode,
            city=_extract_city(loc_text, postcode),
            raw={"loc_text": loc_text},
        )
        if criteria.matches(lst):
            out.append(lst)

    return out
