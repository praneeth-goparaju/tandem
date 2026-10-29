from __future__ import annotations

import asyncio
import logging

from ..models import Listing, SearchCriteria
from . import immowelt, kleinanzeigen

log = logging.getLogger(__name__)

ALL_SCRAPERS = {
    "kleinanzeigen": kleinanzeigen.search,
    "immowelt": immowelt.search,
}


async def search_all_reporting(
    criteria: SearchCriteria, enabled: list[str] | None = None
) -> tuple[list[Listing], dict[str, str]]:
    """Fan out to every enabled scraper in parallel.

    Returns the listings and, by source, why a source could not be searched
    (blocked, timed out, a page it cannot read), so an empty result from a
    failed source is never mistaken for "no listings".
    """
    enabled = enabled or list(ALL_SCRAPERS.keys())
    names = [name for name in enabled if name in ALL_SCRAPERS]
    failures: dict[str, str] = {}

    async def _run(name: str) -> list[Listing]:
        try:
            listings = await asyncio.wait_for(ALL_SCRAPERS[name](criteria), timeout=30)
            log.info(f"{name}: {len(listings)} listings")
            return listings
        except Exception as e:  # noqa: BLE001
            failures[name] = str(e) or type(e).__name__
            log.warning(f"{name}: scrape failed — {failures[name]}")
            return []

    nested = await asyncio.gather(*(_run(name) for name in names))
    return [listing for group in nested for listing in group], failures

