from __future__ import annotations

from typing import Any

from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    partial_success,
    permanent_failure,
)
from src.brain.mcp.tools import mail_ops


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    provider = payload.get("provider")
    uid = payload.get("uid")
    if not provider or not uid:
        return {"ok": False, "result": "missing provider or uid"}
    if any(
        key not in payload or not isinstance(payload[key], str)
        for key in mail_ops.MAIL_SNAPSHOT_KEYS
    ):
        return permanent_failure("approved mail snapshot is missing or invalid")
    snapshot = {key: payload[key] for key in mail_ops.MAIL_SNAPSHOT_KEYS}
    try:
        result = await mail_ops.archive_message(
            provider,
            uid,
            payload.get("mailbox") or "INBOX",
            expected_snapshot=snapshot,
        )
    except mail_ops.MailArchiveAmbiguousError as exc:
        return ambiguous_failure(f"archive outcome is unknown: {exc}")
    except mail_ops.MailArchivePartialError as exc:
        return partial_success(f"archive partially completed: {exc}")
    except Exception as e:  # noqa: BLE001
        return failure_from_exception("archive failed", e)
    return {"ok": True, "result": result}
