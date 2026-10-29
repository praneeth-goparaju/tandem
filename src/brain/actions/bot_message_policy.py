from __future__ import annotations

import re
from typing import Any

from src.brain.slack_text import sanitize_slack_text

MAX_BOT_MESSAGE_CHARS = 4_000
MAX_BOT_TITLE_CHARS = 120
MAX_BRAIN_AREA_CHARS = 64
BRAIN_AREA_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_BRAIN_AREA_RE = re.compile(BRAIN_AREA_PATTERN)


def normalize_bot_message_payload(
    payload: dict[str, Any],
) -> tuple[str, str, str]:
    destination_area = payload.get("destination_area")
    message = payload.get("message")
    title = payload.get("title", "")
    if not isinstance(destination_area, str) or not _BRAIN_AREA_RE.fullmatch(destination_area):
        raise ValueError("area must name one configured Brain area")
    if not isinstance(message, str):
        raise ValueError("message must be a non-empty string")
    if len(message) > MAX_BOT_MESSAGE_CHARS:
        raise ValueError(f"message must be at most {MAX_BOT_MESSAGE_CHARS} characters")
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    if len(title) > MAX_BOT_TITLE_CHARS:
        raise ValueError(f"title must be at most {MAX_BOT_TITLE_CHARS} characters")
    canonical_message = sanitize_slack_text(message).strip()
    canonical_title = sanitize_slack_text(title).strip()
    if not canonical_message:
        raise ValueError("message must contain visible text")
    return destination_area, canonical_message, canonical_title
