from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    is_transient_exception,
    partial_success,
    retryable_failure,
)
from src.brain.mcp._common import notifier
from src.brain.notifications_audit import record_notification
from src.brain.plugins.base import media_outcome_note

_SLACK_IDEMPOTENCY_NAMESPACE = uuid.UUID("198892fb-515a-52f1-9e9b-0f18db775d65")


def _delivery_key(action_id: str, part: str) -> str | None:
    if not action_id:
        return None
    return str(uuid.uuid5(_SLACK_IDEMPOTENCY_NAMESPACE, f"{action_id}:{part}"))


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("title", "")
    message = payload.get("message", "")
    thread_detail = payload.get("thread_detail", "")
    agent = payload.get("_origin_agent") or payload.get("agent") or ""
    area = payload.get("_origin_area") or payload.get("area") or agent or None
    finding_id = payload.get("finding_id")
    proposal_action_ids = payload.get("proposal_action_ids")
    if not isinstance(proposal_action_ids, list):
        proposal_action_ids = []
    media_ids = payload.get("media_ids")
    media_kwargs = {"media_ids": media_ids} if isinstance(media_ids, list) and media_ids else {}
    action_id = str(payload.get("_action_id") or "").strip()
    occasion_fields = ("occasion_person_id", "occasion", "occasion_year")
    present_occasion_fields = [field in payload for field in occasion_fields]
    if any(present_occasion_fields) and not all(present_occasion_fields):
        return {"ok": False, "result": "invalid occasion reminder provenance"}
    reminder_receipt = (
        {field: payload[field] for field in occasion_fields}
        if all(present_occasion_fields)
        else None
    )
    if reminder_receipt:
        from src.brain.occasions import store as occasion_store

        try:
            action_date = occasion_store.local_date(
                datetime.fromisoformat(str(payload["_action_created_at"]))
            )
            reminder_year = int(reminder_receipt["occasion_year"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "result": "invalid occasion reminder provenance"}
        if (
            not isinstance(reminder_receipt["occasion_person_id"], str)
            or not str(reminder_receipt["occasion_person_id"]).strip()
            or not isinstance(reminder_receipt["occasion"], str)
            or not str(reminder_receipt["occasion"]).strip()
            or not isinstance(reminder_receipt["occasion_year"], int)
            or isinstance(reminder_receipt["occasion_year"], bool)
            or payload.get("_origin_agent") != "humans"
            or payload.get("_origin_area") != "humans"
            or not action_id
        ):
            return {"ok": False, "result": "invalid occasion reminder provenance"}
        reminder_status = occasion_store.reminder_identity_status(
            reminder_receipt["occasion_person_id"],
            reminder_receipt["occasion"],
            reminder_year,
            created_on=action_date,
            executing_on=occasion_store.local_date(),
        )
        if reminder_status == "already_reminded":
            return {
                "ok": True,
                "result": "occasion reminder already delivered; skipped duplicate",
            }
        if reminder_status != "due":
            return {"ok": False, "result": f"{reminder_status} occasion reminder identity"}

    n = notifier()
    persona = getattr(n, "persona_for", lambda *_: None)(area, agent)
    full_title = title if persona else (f"[{agent}] {title}" if agent and title else title)

    try:
        result = await n.send(
            message, title=full_title, urgency="info",
            finding_id=finding_id, area=area, agent=agent,
            idempotency_key=_delivery_key(action_id, "root"),
            proposal_action_ids=proposal_action_ids,
            require_explicit_area=True,
            **media_kwargs,
        )
        status = result.get("status", "unknown")
    except Exception as e:  # noqa: BLE001
        if not action_id and is_transient_exception(e):
            return ambiguous_failure(
                f"notifier outcome unknown without an idempotency key: {e}"
            )
        return failure_from_exception("notifier error", e)

    if status in {"sent", "noop"}:
        ts = result.get("id") or ""
        record_notification(
            agent=agent or "unknown",
            area=area,
            title=full_title,
            message=message,
            status=status,
            signal_ts=ts,
            finding_id=finding_id,
            group_id=result.get("group_id"),
        )
        thread_status = ""
        thread_ts = ""
        thread_failure = ""
        if thread_detail and ts:
            try:
                thread_result = await n.send(
                    thread_detail,
                    title="",
                    urgency="info",
                    finding_id=finding_id,
                    area=area,
                    agent=agent,
                    quote_timestamp=ts,
                    idempotency_key=_delivery_key(action_id, "detail"),
                    require_explicit_area=True,
                )
                thread_status = thread_result.get("status", "unknown")
                thread_ts = thread_result.get("id") or ""
                if thread_status in {"sent", "noop"}:
                    record_notification(
                        agent=agent or "unknown",
                        area=area,
                        title="(thread detail)",
                        message=thread_detail,
                        status=thread_status,
                        signal_ts=thread_result.get("id"),
                        finding_id=finding_id,
                        group_id=thread_result.get("group_id"),
                    )
                else:
                    thread_failure = str(
                        thread_result.get("error") or f"notifier:{thread_status}"
                    )
                    record_notification(
                        agent=agent or "unknown",
                        area=area,
                        title="(thread detail)",
                        message=thread_detail,
                        status="failed",
                        signal_ts=None,
                        finding_id=finding_id,
                        group_id=thread_result.get("group_id") or result.get("group_id"),
                    )
            except Exception as e:  # noqa: BLE001
                thread_status = f"error:{e}"
                thread_failure = f"{type(e).__name__}: {e}"
                record_notification(
                    agent=agent or "unknown",
                    area=area,
                    title="(thread detail)",
                    message=thread_detail,
                    status="failed",
                    signal_ts=None,
                    finding_id=finding_id,
                    group_id=result.get("group_id"),
                )
        suffix = f" thread={thread_status}" if thread_detail else ""
        if reminder_receipt and status != "sent":
            return {"ok": False, "result": "occasion reminder was not delivered to Slack"}
        if reminder_receipt and not ts:
            return partial_success(
                "occasion reminder root may have been delivered without a receipt; "
                "dedupe was not stamped"
            )
        if reminder_receipt and thread_detail and (
            thread_status != "sent" or not thread_ts
        ):
            thread_failure = thread_failure or (
                f"thread status was {thread_status or 'missing'} without a receipt"
            )
        media_note = media_outcome_note(result)
        media_suffix = f"\n{media_note}" if media_note else ""
        if thread_failure:
            return partial_success(
                f"notifier:{status} ts={ts}{suffix}; detail not delivered: {thread_failure}"
                f"{media_suffix}"
            )
        if reminder_receipt:
            from src.brain.occasions.store import stamp_reminded

            try:
                stamped = stamp_reminded(
                    str(reminder_receipt["occasion_person_id"]),
                    str(reminder_receipt["occasion"]),
                    reminder_year,
                    ts=str(ts),
                )
            except Exception as exc:  # noqa: BLE001
                stamped = False
                stamp_error = f"{type(exc).__name__}: {exc}"
            else:
                stamp_error = "person disappeared before the receipt was recorded"
            if not stamped:
                return partial_success(
                    f"notifier:{status} ts={ts}{suffix}; occasion reminder delivered "
                    f"but receipt failed: {stamp_error}"
                )
        delivered = f"notifier:{status} ts={ts}{suffix}{media_suffix}"
        if result.get("failed_media"):
            return partial_success(delivered)
        return {"ok": True, "result": delivered}
    reason = str(result.get("error") or f"notifier:{status}")
    if result.get("ambiguous"):
        return ambiguous_failure(reason)
    if result.get("retryable"):
        if not action_id:
            return ambiguous_failure(
                f"notifier outcome unknown without an idempotency key: {reason}"
            )
        return retryable_failure(reason)
    return {"ok": False, "result": reason}
