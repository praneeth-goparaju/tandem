from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.orchestrator.config import HOUSING_CORRIDOR as _HC

TARGET_POSTCODES: set[str] = _HC.postcodes if _HC.configured else set()

TARGET_CITIES: set[str] = _HC.city_aliases if _HC.configured else set()


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price_eur: int | None = None
    rooms: float | None = None
    sqm: float | None = None
    address: str = ""
    postcode: str = ""
    city: str = ""
    lat: float | None = None
    lon: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass
class SearchCriteria:
    area: str = ""
    max_price: int | None = None
    min_rooms: float | None = None
    min_sqm: float | None = None
    property_type: str = "house"

    def matches(self, listing: Listing) -> bool:
        if self.max_price is not None and listing.price_eur is not None:
            if listing.price_eur > self.max_price:
                return False
        if self.min_rooms is not None and listing.rooms is not None:
            if listing.rooms < self.min_rooms:
                return False
        if self.min_sqm is not None and listing.sqm is not None:
            if listing.sqm < self.min_sqm:
                return False
        return True
