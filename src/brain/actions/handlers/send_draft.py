from __future__ import annotations

import asyncio
import hmac
import uuid
from typing import Any

from src.brain.actions.fingerprints import is_sha256_hex
from src.brain.actions.handlers._draft_io import (
    extract_body,
    frontmatter_field,
    mark_sent_and_copy,
    read_draft_file,
)
from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    is_transient_exception,
    partial_success,
    permanent_failure,
    retryable_failure,
)
from src.brain.plugins.base import DELIVERY_SUCCESS_STATES

_SLACK_IDEMPOTENCY_NAMESPACE = uuid.UUID("8064208d-346e-5166-9c31-558fcbd14e0a")


def _delivery_key(action_id: str) -> str | None:
    if not action_id:
        return None
    return str(uuid.uuid5(_SLACK_IDEMPOTENCY_NAMESPACE, action_id))


async def _send_via_notifier(
    body: str,
    recipient: str | None,
    area: str | None,
    *,
    action_id: str,
) -> dict:
    from src.brain.mcp._common import notifier

    target_area = recipient or area
    return await notifier().send(
        body,
        title="",
        urgency="info",
        area=target_area,
        idempotency_key=_delivery_key(action_id),
    )


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    from src.orchestrator.config import VAULT_PATH

    rel = str(payload.get("draft_path") or "").strip()
    if not rel:
        return {"ok": False, "result": "missing draft_path"}

    expected_sha256 = str(payload.get("draft_sha256") or "").lower()
    try:
        expected_size = int(payload["draft_size"])
    except (KeyError, TypeError, ValueError):
        return permanent_failure("approved draft snapshot is missing size metadata")
    if not is_sha256_hex(expected_sha256):
        return permanent_failure("approved draft snapshot is missing SHA-256 metadata")

    try:
        src, data, actual_sha256 = await asyncio.to_thread(
            read_draft_file, VAULT_PATH, rel
        )
        text = data.decode("utf-8")
    except (OSError, PermissionError, UnicodeDecodeError, ValueError) as exc:
        return permanent_failure(f"draft validation failed: {exc}")
    if frontmatter_field(text, "status") == "sent":
        marker = frontmatter_field(text, "approved_sha256") or ""
        if hmac.compare_digest(marker, expected_sha256):
            return {"ok": True, "result": "already sent (idempotent no-op)"}
        return permanent_failure("draft is marked sent under a different approval snapshot")
    if len(data) != expected_size or not hmac.compare_digest(
        actual_sha256, expected_sha256
    ):
        return permanent_failure("draft file content changed after approval")

    kind = frontmatter_field(text, "kind") or "doc"
    action_id = str(payload.get("_action_id") or "").strip()

    if kind == "message":
        recipient = frontmatter_field(text, "recipient")
        area = frontmatter_field(text, "area")
        body = extract_body(text)
        origin_area = str(payload.get("_origin_area") or "").strip()
        if origin_area:
            recipient, area = origin_area, None
        try:
            result = await _send_via_notifier(
                body,
                recipient,
                area,
                action_id=action_id,
            )
        except Exception as exc:  # noqa: BLE001
            if not action_id and is_transient_exception(exc):
                return ambiguous_failure(
                    "notifier outcome unknown without an idempotency key: "
                    f"{type(exc).__name__}: {exc}"
                )
            return failure_from_exception("notifier error", exc)
        if result.get("status") not in DELIVERY_SUCCESS_STATES:
            reason = f"send failed: {result.get('error') or result}"
            if result.get("ambiguous"):
                return ambiguous_failure(reason)
            if result.get("retryable"):
                if not action_id:
                    return ambiguous_failure(
                        f"notifier outcome unknown without an idempotency key: {reason}"
                    )
                return retryable_failure(reason)
            return permanent_failure(reason)
        try:
            copy_rel = await asyncio.to_thread(
                mark_sent_and_copy,
                src,
                VAULT_PATH,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        except (OSError, PermissionError, ValueError) as e:
            return partial_success(
                f"sent (id={result.get('id', '?')}); draft bookkeeping failed: "
                f"{type(e).__name__}: {e}"
            )
        return {"ok": True, "result": f"sent (id={result.get('id', '?')}, archived → {copy_rel})"}

    try:
        copy_rel = await asyncio.to_thread(
            mark_sent_and_copy,
            src,
            VAULT_PATH,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
    except (OSError, PermissionError, ValueError) as exc:
        return permanent_failure(f"draft changed before bookkeeping: {exc}")
    return {"ok": True, "result": f"sent (file copy → {copy_rel}, kind={kind})"}
