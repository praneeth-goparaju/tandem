from __future__ import annotations

import logging
import uuid
from typing import Any

from src.brain.actions.bot_message_policy import normalize_bot_message_payload
from src.brain.actions.outcomes import (
    ambiguous_failure,
    is_transient_exception,
    permanent_failure,
    retryable_failure,
    success,
)
from src.brain.mcp._common import notifier
from src.brain.notifications_audit import record_notification

log = logging.getLogger(__name__)
_SLACK_IDEMPOTENCY_NAMESPACE = uuid.UUID("1f00d22a-858c-5f37-b07f-8a877f19ef3d")


def _delivery_key(action_id: str) -> str | None:
    if not action_id:
        return None
    return str(uuid.uuid5(_SLACK_IDEMPOTENCY_NAMESPACE, action_id))


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        destination_area, message, title = normalize_bot_message_payload(payload)
    except ValueError as exc:
        return permanent_failure(str(exc))

    action_id = str(payload.get("_action_id") or "").strip()
    origin_agent = str(payload.get("_origin_agent") or "external-mcp").strip()
    n = notifier()
    route_lookup = getattr(n, "channel_for_area", None)
    if not callable(route_lookup):
        return permanent_failure("the configured notifier does not support Brain area routes")
    try:
        channel = route_lookup(destination_area, allow_default=False)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "Brain bot route lookup failed for area=%s: %s",
            destination_area,
            type(exc).__name__,
        )
        return permanent_failure("Brain area route lookup failed")
    if not isinstance(channel, str) or not channel.strip():
        return permanent_failure(
            f"no configured Brain Slack route exists for area '{destination_area}'"
        )

    try:
        result = await n.send(
            message,
            title=title,
            urgency="info",
            finding_id=None,
            area=destination_area,
            agent=origin_agent,
            idempotency_key=_delivery_key(action_id),
            plain_text=True,
            proposal_action_ids=[],
            require_explicit_area=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "Brain bot delivery raised for area=%s: %s",
            destination_area,
            type(exc).__name__,
        )
        if not action_id and is_transient_exception(exc):
            return ambiguous_failure("Slack delivery outcome is unknown without an idempotency key")
        if is_transient_exception(exc):
            return retryable_failure("temporary Slack bot delivery failure")
        return permanent_failure("Slack bot delivery failed")

    status = str(result.get("status") or "unknown")
    timestamp = str(result.get("id") or "")
    if status in {"sent", "noop"}:
        record_notification(
            agent=origin_agent or "external-mcp",
            area=destination_area,
            title=title,
            message=message,
            status=status,
            signal_ts=timestamp,
            finding_id=None,
            group_id=result.get("group_id"),
        )
        suffix = f" ts={timestamp}" if timestamp else ""
        return success(f"brain-bot:{status} area={destination_area}{suffix}")

    if result.get("ambiguous"):
        return ambiguous_failure("Slack bot delivery outcome is unknown")
    if result.get("retryable"):
        if not action_id:
            return ambiguous_failure("Slack delivery outcome is unknown without an idempotency key")
        return retryable_failure("temporary Slack bot delivery failure")
    return permanent_failure("Slack bot delivery failed")
