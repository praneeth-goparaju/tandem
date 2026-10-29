from __future__ import annotations

import hashlib
import json
from typing import Any


def is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value.casefold()
    )


def payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def dedupe_key(
    *,
    action_type: str,
    payload: Any,
    origin_area: str | None,
    version: int,
    occurrence_key: str | None = None,
) -> str:
    """Return the stable identity of one logical action occurrence.

    With no occurrence key, equivalent payloads in the same area coalesce.
    Callers that intentionally repeat the same payload provide a stable
    business/occurrence key (for example ``daily-brief:2026-08-23``). A retry
    of that occurrence still deduplicates, while a different occurrence does
    not.
    """
    source: dict[str, Any] = {
        "origin_area": origin_area,
        "payload": payload,
        "type": action_type,
        "version": version,
    }
    if occurrence_key is not None:
        source["occurrence_key"] = occurrence_key
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
