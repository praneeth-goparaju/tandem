from __future__ import annotations

import logging
import re

from src.brain.actions import queue

log = logging.getLogger(__name__)

_ACTION_ID = r"([a-f0-9]{12})"
_COMMAND_RE = re.compile(
    rf"^\s*(yes|approve|approved|no|reject|rejected)\s+`?{_ACTION_ID}`?\s*$",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    rf"^\s*`?{_ACTION_ID}`?\s*"
    r"(✅|:white_check_mark:|:heavy_check_mark:|❌|:x:)\s*$",
    re.IGNORECASE,
)
_DECISION_MAP = {
    "yes": "approved",
    "approve": "approved",
    "approved": "approved",
    "no": "rejected",
    "reject": "rejected",
    "rejected": "rejected",
    "✅": "approved",
    ":white_check_mark:": "approved",
    ":heavy_check_mark:": "approved",
    "❌": "rejected",
    ":x:": "rejected",
}


def parse(text: str) -> tuple[str, str] | None:
    value = text or ""
    command = _COMMAND_RE.fullmatch(value)
    if command:
        word, action_id = command.group(1).lower(), command.group(2).lower()
        return _DECISION_MAP[word], action_id
    emoji = _EMOJI_RE.fullmatch(value)
    if emoji:
        action_id, marker = emoji.group(1).lower(), emoji.group(2).lower()
        return _DECISION_MAP[marker], action_id
    return None


def handle_reply(
    text: str,
    *,
    area: str | None,
    actor: str,
    owner: bool = False,
) -> bool:
    """Interpret a human reply and authorize it against the queued action.

    Area-scoped actions must be approved in their originating area. Legacy
    rows without provenance are deliberately owner-only.
    """
    parsed = parse(text)
    if not parsed:
        return False
    decision, action_id = parsed
    row = queue.get(action_id)
    if row is None:
        log.info("approval reply: action %s not found", action_id)
        return True
    origin_area = row.get("origin_area")
    audience = row.get("approval_audience") or "owner"
    authorized = (
        bool(origin_area)
        and area == origin_area
        and audience == f"area:{origin_area}"
    ) or (not origin_area and owner and audience == "owner")
    if not authorized:
        log.warning(
            "approval reply denied: action=%s origin_area=%r reply_area=%r "
            "audience=%r actor=%s",
            action_id,
            origin_area,
            area,
            audience,
            actor,
        )
        return True
    moved = queue.transition_status(
        action_id,
        "pending",
        decision,
        channel="slack_reply",
        signal=text.strip(),
        actor=actor,
        expected_origin_area=origin_area,
        enforce_origin_area=True,
    )
    if moved:
        log.info("approval reply: action %s → %s", action_id, decision)
    else:
        log.info("approval reply: action %s no-op (already resolved)", action_id)
    return True
