"""Immowelt — houses for sale.

The Immowelt SERP is now built on their "UFRN" micro-frontend rather than
Next.js. The page embeds the search payload in a <script id="__UFRN_FETCHER__">
tag whose body is `JSON.parse("…escaped…")`. The interesting field is
`data["classified-serp-init-data"]`, which is an `lz-string`-compressed
base64 string (signature `N4Ig…`). After decompression you get a JSON dict
shaped like::

    pageProps:
      classifiedsData:
        <classifiedId>:
          hardFacts: { facts: [{type, splitValue, label}], price: {value, …} }
          rawData:   { price, propertySubType, geoIdHierarchy, … }
          location:  { address: { zipCode, city, street, district } }
          url:       "https://www.immowelt.de/expose/<uuid>"
          mainDescription: "…"
          energyClass: "C" | None

So this scraper:
  1. Fetches the SERP with the correct geo id (see GEO_IDS below).
  2. Pulls the UFRN_FETCHER blob, JSON-unescapes, then lz-string-decompresses.
  3. Walks classifiedsData and maps each entry → Listing.

Refreshing geo IDs: visit
    https://www.immowelt.de/suche/<city>/haeuser/kaufen
and follow the 301 redirects. The terminal URL is
`/suche/kaufen/haus/<state>/<city>-<plz>/<geo_id>` — the geo_id is the
lowercase path segment. Past hardcoded values were silently wrong (geo
ids drift), so cross-check `location.address.city` of the first few
results when bumping the entries in `housing_corridor.immowelt_geo_ids`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import lzstring

from src.orchestrator.config import HOUSING_CORRIDOR as _HC

from ..models import Listing, SearchCriteria
from . import _common

log = logging.getLogger(__name__)

BASE = "https://www.immowelt.de"


def _build_geo_ids() -> dict[str, str]:
    if not _HC.configured:
        return {}
    canonical_map = _HC.immowelt_geo_ids
    alias_groups = _HC.city_alias_groups
    out: dict[str, str] = {}
    for canonical, geo_id in canonical_map.items():
        out[canonical] = geo_id
        for alt in alias_groups.get(canonical, []):
            out[alt] = geo_id
    return out


GEO_IDS: dict[str, str] = _build_geo_ids()

DEFAULT_RADIUS_KM = _HC.immowelt_default_radius_km if _HC.configured else 0

_FETCHER_RE = re.compile(
    r'<script[^>]*id="__UFRN_FETCHER__"[^>]*>(.*?)</script>', re.DOTALL
)
_JSON_PARSE_RE = re.compile(r'JSON\.parse\("(.*)"\)', re.DOTALL)


def _build_url(criteria: SearchCriteria) -> str:
    key = criteria.area.strip().lower()
    geo_id = GEO_IDS.get(key, "")
    estate_types = "House" if criteria.property_type == "house" else "House,Apartment"
    parts = [
        "distributionTypes=Buy",
        f"estateTypes={estate_types}",
        f"radius={DEFAULT_RADIUS_KM}",
    ]
    if geo_id:
        parts.append(f"locations={geo_id}")
    else:
        parts.append(f"searchQuery={key}")
        log.warning(f"immowelt: no geo_id for {key!r}; SERP may be empty")
    if criteria.max_price:
        parts.append(f"priceMax={criteria.max_price}")
    if criteria.min_rooms:
        parts.append(f"roomsMin={criteria.min_rooms}")
    if criteria.min_sqm:
        parts.append(f"livingSpaceMin={int(criteria.min_sqm)}")
    return f"{BASE}/classified-search?{'&'.join(parts)}"


def _extract_classifieds(html: str) -> dict[str, dict]:
    m = _FETCHER_RE.search(html)
    if not m:
        log.warning("immowelt: __UFRN_FETCHER__ not found — page layout may have changed")
        return {}
    body = m.group(1)

    js = _JSON_PARSE_RE.search(body)
    if not js:
        log.warning("immowelt: JSON.parse(...) wrapper not found inside __UFRN_FETCHER__")
        return {}

    try:
        # Two levels of unescape: first the JS string, then the JSON it contained.
        unescaped = json.loads('"' + js.group(1) + '"')
        outer = json.loads(unescaped)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"immowelt: failed to parse fetcher JSON: {e}")
        return {}

    blob = (outer.get("data") or {}).get("classified-serp-init-data")
    if not blob:
        log.info("immowelt: no classified-serp-init-data — likely no geo_id match")
        return {}

    try:
        decoded = lzstring.LZString().decompressFromBase64(blob)
    except Exception as e:  # noqa: BLE001
        log.warning(f"immowelt: lz-string decompression failed: {e}")
        return {}
    if not decoded:
        log.warning("immowelt: lz-string decompression returned empty")
        return {}

    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as e:
        log.warning(f"immowelt: decoded payload is not valid JSON: {e}")
        return {}

    cdata = (payload.get("pageProps") or {}).get("classifiedsData") or {}
    if not isinstance(cdata, dict):
        log.warning("immowelt: classifiedsData is not a dict")
        return {}
    return cdata


def _hardfact(facts: list[dict], type_: str) -> str | None:
    for f in facts or []:
        if isinstance(f, dict) and f.get("type") == type_:
            v = f.get("splitValue")
            if v:
                return str(v)
    return None


def _parse_classified(cid: str, c: dict) -> Listing | None:
    hf = c.get("hardFacts") or {}
    facts = hf.get("facts") or []
    title = hf.get("title") or ""

    # Price: rawData.price is the numeric one (hardFacts.price.value is "395.000 €").
    raw = c.get("rawData") or {}
    price_raw = raw.get("price")
    price = int(price_raw) if isinstance(price_raw, (int, float)) else None
    if price is None:
        price_obj = hf.get("price") or {}
        price = _common.parse_price_eur(price_obj.get("value") or price_obj.get("formatted"))

    rooms_str = _hardfact(facts, "numberOfRooms")
    rooms = float(rooms_str.replace(",", ".")) if rooms_str else None
    if rooms is None and isinstance(raw.get("nbroom"), (int, float)):
        rooms = float(raw["nbroom"])

    sqm_str = _hardfact(facts, "livingSpace")
    sqm = float(sqm_str.replace(",", ".")) if sqm_str else None
    if sqm is None and isinstance(raw.get("surface"), (int, float)):
        sqm = float(raw["surface"])

    addr = (c.get("location") or {}).get("address") or {}
    postcode = str(addr.get("zipCode") or "")
    city = str(addr.get("city") or "")
    address = " ".join(
        str(x) for x in (addr.get("street"), postcode, city, addr.get("district")) if x
    ).strip()

    url = c.get("url") or f"{BASE}/expose/{cid}"

    energy_class = c.get("energyClass") or ""
    desc = c.get("mainDescription")
    if isinstance(desc, dict):
        desc = desc.get("text") or desc.get("description") or ""
    elif not isinstance(desc, str):
        desc = ""
    raw_blob: dict[str, Any] = {
        "energyClass": energy_class,
        "propertySubType": raw.get("propertySubType"),
        "mainDescription": desc[:600],
    }

    return Listing(
        source="immowelt",
        source_id=cid,
        url=url,
        title=title,
        price_eur=price,
        rooms=rooms,
        sqm=sqm,
        address=address,
        postcode=postcode,
        city=city,
        raw=raw_blob,
    )


async def search(criteria: SearchCriteria) -> list[Listing]:
    url = _build_url(criteria)
    async with _common.client() as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}" + (" (blocked)" if resp.status_code == 403 else ""))
        html = resp.text

    cdata = _extract_classifieds(html)
    out: list[Listing] = []
    for cid, classified in cdata.items():
        try:
            lst = _parse_classified(cid, classified)
        except Exception as e:  # noqa: BLE001
            log.debug(f"immowelt: skipping {cid}: {e}")
            continue
        if lst and criteria.matches(lst):
            out.append(lst)
    return out
