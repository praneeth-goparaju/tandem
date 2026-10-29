from __future__ import annotations

import json

from src.brain.housing import require_configured
from src.brain.housing.models import SearchCriteria
from src.brain.housing.search import run_search_reporting
from src.brain.housing.storage import (
    delete_watch,
    get_db,
    list_watches,
    save_watch,
)
from src.brain.mcp._common import _bracket, _wrap_external
from src.brain.mcp.caller_context import require_current_caller
from src.brain.mcp.tool_registry import brain_tool
from src.brain.net_security import safe_https_url
from src.orchestrator.config import HOUSING_CORRIDOR as _HC

_CORRIDOR_CITIES = _HC.cities if _HC.configured else []
_DEFAULT_AREA = (_CORRIDOR_CITIES[0] if _CORRIDOR_CITIES else "").lower()
_CORRIDOR_LABEL = _HC.description() if _HC.configured else "configured corridor"
_CITY_HINT = ", ".join(repr(c.lower()) for c in _CORRIDOR_CITIES[:3]) or "the configured city names"
_LISTING_HOSTS = {
    "immowelt": frozenset({"immowelt.de", "www.immowelt.de"}),
    "kleinanzeigen": frozenset({"kleinanzeigen.de", "www.kleinanzeigen.de"}),
}


def _safe_listing_url(source: str, value: str) -> str:
    allowed = _LISTING_HOSTS.get(str(source), frozenset())
    return safe_https_url(value, allowed_hosts=allowed) or "(invalid source URL)"


def _listing_block(lst, *, compact: bool = False) -> str:
    price = f"€{lst.price_eur:,}" if lst.price_eur else "price?"
    size = f"{lst.sqm:.0f}m²" if lst.sqm else "sqm?"
    rooms = f"{lst.rooms:g} Zi" if lst.rooms else "rooms?"
    location = " ".join(part for part in (lst.postcode, lst.city) if part)
    fields = [
        f"id: {lst.id}",
        f"source: {lst.source}",
        f"title: {lst.title[:55] if compact else lst.title}",
        f"price: {price}",
        f"rooms: {rooms}",
        f"size: {size}",
        f"location: {location or '?'}",
        f"url: {_safe_listing_url(lst.source, lst.url)}",
    ]
    return _wrap_external("\n".join(fields), source="housing/listing")


def _criteria_from_args(args: dict) -> SearchCriteria:
    return SearchCriteria(
        area=args.get("area", _DEFAULT_AREA),
        max_price=args.get("max_price"),
        min_rooms=args.get("min_rooms"),
        min_sqm=args.get("min_sqm"),
        property_type=args.get("property_type", "house"),
    )


@brain_tool(
    name="search_houses",
    description=(
        f"Search houses and apartments for sale in the configured corridor "
        f"({_CORRIDOR_LABEL}). Scrapes Kleinanzeigen and Immowelt, then "
        f"dedupes cross-site hits. Results "
        f"are filtered to the target postcodes by default. Use the local "
        f"city names ({_CITY_HINT})."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "area": {"type": "string", "description": f"City name from the configured corridor (default: {_DEFAULT_AREA or 'first configured city'})"},
            "max_price": {"type": "integer", "description": "Maximum price in EUR"},
            "min_rooms": {"type": "number", "description": "Minimum number of rooms"},
            "min_sqm": {"type": "number", "description": "Minimum living area (m²)"},
            "property_type": {"type": "string", "enum": ["house", "apartment", "any"], "description": "Default: house"},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "Subset of: kleinanzeigen, immowelt. Default: both."},
            "strict_corridor": {"type": "boolean", "description": "Filter to target postcodes (default: true)"},
        },
    },
)
async def search_houses(args):
    require_configured()
    criteria = _criteria_from_args(args)
    listings, failures = await run_search_reporting(
        criteria,
        sources=args.get("sources"),
        strict_corridor=args.get("strict_corridor", True),
    )
    failed = ", ".join(f"{source} ({reason})" for source, reason in sorted(failures.items()))
    if not listings:
        if failures:
            return {"content": [{"type": "text", "text": (
                f"The search did not work: these sources could not be searched: {failed}. "
                "This is not a result: say the search failed, not that there are no listings."
            )}]}
        return {"content": [{"type": "text", "text": f"No listings for area={criteria.area} max_price={criteria.max_price}."}]}

    lines = [f"Found {len(listings)} listings:"]
    if failures:
        lines.append(f"Not searched, so listings there are missing: {failed}.")
    for lst in listings[:40]:
        lines.append("\n" + _listing_block(lst))
    if len(listings) > 40:
        lines.append(f"\n… and {len(listings) - 40} more.")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@brain_tool(
    name="watch_search",
    description=(
        "Save a named house search. The daily scheduler re-runs saved searches "
        "and sends Slack alerts to #housing for any newly-appearing listings."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short name for this watch (e.g. 'family-home-musterstadt')"},
            "area": {"type": "string"},
            "max_price": {"type": "integer"},
            "min_rooms": {"type": "number"},
            "min_sqm": {"type": "number"},
            "property_type": {"type": "string", "enum": ["house", "apartment", "any"]},
        },
        "required": ["name"],
    },
)
async def watch_search(args):
    require_configured()
    caller = require_current_caller()
    if not caller.is_reply:
        raise PermissionError("housing watches can change only during an authenticated reply")
    name = args["name"]
    criteria = _criteria_from_args(args)
    save_watch(get_db(), name, vars(criteria))
    return {"content": [{
        "type": "text",
        "text": f"Watch {_bracket(name)} saved. Criteria: {_bracket(json.dumps(vars(criteria), ensure_ascii=False))}",
    }]}


@brain_tool(
    name="list_watched_searches",
    description="List all saved house-search watches and their criteria.",
    input_schema={"type": "object", "properties": {}},
)
async def list_watched_searches(args):
    require_configured()
    rows = list_watches(get_db())
    if not rows:
        return {"content": [{"type": "text", "text": "No watches saved."}]}
    lines = [f"{len(rows)} watches:"]
    for w in rows:
        record = (
            f"name: {w['name']}\n"
            f"criteria: {json.dumps(w['criteria'], ensure_ascii=False)}\n"
            f"last run: {w['last_run_at'] or 'never'}"
        )
        lines.append("\n" + _wrap_external(record, source="housing/watch"))
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@brain_tool(
    name="process_watches",
    description=(
        "Run every saved housing watch: scrape sources, dedup, record per-watch "
        "hits, and return the new listings per watch. It never posts directly; "
        "the housing agent decides which new listings are worth reporting."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def process_watches(args):
    require_configured()
    from src.brain.housing.search import run_watches

    results = await run_watches()
    if not results:
        return {"content": [{"type": "text", "text": "No watches saved — nothing to process."}]}
    lines = []
    for watch_name, new_listings in results.items():
        if new_listings:
            lines.append(
                f"Watch {_bracket(watch_name)}: {len(new_listings)} new listing(s) ready for review"
            )
            for lst in new_listings:
                lines.append(_listing_block(lst, compact=True))
        else:
            lines.append(
                f"Watch {_bracket(watch_name)}: 0 new (all listings already seen in prior runs)"
            )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@brain_tool(
    name="unwatch_search",
    description="Remove a saved house-search watch by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)
async def unwatch_search(args):
    require_configured()
    caller = require_current_caller()
    if not caller.is_reply:
        raise PermissionError("housing watches can change only during an authenticated reply")
    deleted = delete_watch(get_db(), args["name"])
    safe_name = _bracket(args["name"])
    msg = f"Removed watch {safe_name}." if deleted else f"No watch named {safe_name}."
    return {"content": [{"type": "text", "text": msg}]}
