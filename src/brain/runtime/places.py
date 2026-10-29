from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_PLACES = 60
MAX_PLACE_LENGTH = 80


def owner_places(profile: Mapping[str, Any]) -> list[str]:
    """Place names in `user.locations`, `user.country`, `user.family_locations` and `housing_corridor.city_aliases`."""
    found: list[str] = []
    user = profile.get("user") if isinstance(profile, Mapping) else None
    for key in ("locations", "country", "family_locations"):
        values = user.get(key) if isinstance(user, Mapping) else None
        if key == "country" and isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            found.extend(values)
    corridor = profile.get("housing_corridor") if isinstance(profile, Mapping) else None
    aliases = corridor.get("city_aliases") if isinstance(corridor, Mapping) else None
    if isinstance(aliases, Mapping):
        for names in aliases.values():
            if isinstance(names, list):
                found.extend(names)
    places: list[str] = []
    for item in found:
        if isinstance(item, str) and 0 < len(item.strip()) <= MAX_PLACE_LENGTH and item.strip() not in places:
            places.append(item.strip())
    return places[:MAX_PLACES]


def place_prompt(places: list[str]) -> str | None:
    return f"Places: {', '.join(places)}." if places else None


@lru_cache(maxsize=1)
def configured_places(repo_root: Path) -> tuple[str, ...]:
    from src.brain.runtime.loader import load_yaml, resolve_user_dir

    try:
        profile = load_yaml(resolve_user_dir(repo_root) / "profile.yaml")
    except Exception as exc:  # noqa: BLE001
        log.warning("owner places unavailable: %s", type(exc).__name__)
        return ()
    return tuple(owner_places(profile or {}))
