from __future__ import annotations

import asyncio
import json
import logging

from src.brain.actions import queue
from src.brain.actions.handlers import HANDLERS
from src.brain.actions.outcomes import TransientActionError, is_transient_exception
from src.brain.actions.types import (
    AUTO_ELIGIBLE_TYPES,
    HUMAN_APPROVAL_CHANNELS,
    TRUSTED_CLIENT_ACTION_TYPES,
    TRUSTED_CLIENT_APPROVAL_CHANNEL,
    ActionType,
)

log = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5.0
HANDLER_TIMEOUT_SECS = 60.0
# A batch archives up to 50 mails, a few seconds each even over one session.
HANDLER_TIMEOUT_BY_TYPE = {"mail_archive_batch": 300.0}


def handler_timeout(action_type: object) -> float:
    return HANDLER_TIMEOUT_BY_TYPE.get(str(action_type or ""), HANDLER_TIMEOUT_SECS)
MAX_RETRIES = 2


class _HandlerRaisedTimeout(RuntimeError):
    ...


def _is_transient(e: BaseException) -> bool:
    return is_transient_exception(e)


async def _requeue_or_fail(action_id: str, reason: str, exc: BaseException | None) -> None:
    from src.db import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT retry_count FROM pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    retries = (row[0] or 0) if row else 0
    if retries < MAX_RETRIES and exc is not None and _is_transient(exc):
        conn.execute(
            "UPDATE pending_actions SET retry_count = ?, status = 'approved', "
            "claimed_at = NULL, result = ? WHERE id = ? AND status = 'executing'",
            (retries + 1, f"retry {retries + 1}/{MAX_RETRIES}: {reason}", action_id),
        )
        conn.commit()
        log.info("action %s requeued (retry %d/%d)", action_id, retries + 1, MAX_RETRIES)
        return
    queue.transition_status(action_id, "executing", "failed", result=reason)


async def _run_one(row: dict) -> None:
    action_id = row["id"]
    type_str = row["type"]

    if not queue.transition_status(action_id, "approved", "executing"):
        return

    try:
        atype = ActionType(type_str)
    except ValueError:
        queue.transition_status(action_id, "executing", "failed", result=f"unknown type: {type_str}")
        return

    actor = str(row.get("approval_actor") or "").strip()
    human_approved = (
        row.get("approval_channel") in HUMAN_APPROVAL_CHANNELS and bool(actor)
    )
    trusted_client_approved = (
        atype in TRUSTED_CLIENT_ACTION_TYPES
        and row.get("approval_channel") == TRUSTED_CLIENT_APPROVAL_CHANNEL
        and bool(actor)
    )
    if (
        atype not in AUTO_ELIGIBLE_TYPES
        and not human_approved
        and not trusted_client_approved
    ):
        queue.transition_status(
            action_id, "executing", "failed",
            result=(
                f"refused: {type_str} is not auto-eligible; needs genuine human "
                "approval or a bounded trusted-client grant with actor provenance"
            ),
        )
        return

    handler = HANDLERS.get(atype)
    if handler is None:
        queue.transition_status(action_id, "executing", "failed", result=f"no handler for {type_str}")
        return

    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except (json.JSONDecodeError, TypeError) as e:
        queue.transition_status(
            action_id,
            "executing",
            "failed",
            result=f"invalid stored payload_json: {e}",
        )
        return
    if not isinstance(payload, dict):
        queue.transition_status(
            action_id,
            "executing",
            "failed",
            result="invalid stored payload_json: expected an object",
        )
        return
    payload["_action_id"] = action_id
    payload["_action_created_at"] = row.get("created_at")
    payload["_origin_agent"] = row.get("origin_agent")
    payload["_origin_area"] = row.get("origin_area")

    async def _invoke_handler():
        try:
            return await handler(payload)
        except TimeoutError as exc:
            # A timeout raised *by* the handler means its awaitable settled and
            # can follow the typed transient policy. An asyncio.wait_for
            # deadline below is different: a to_thread worker cannot be killed
            # and may still commit its effect after our awaiter is cancelled.
            raise _HandlerRaisedTimeout(str(exc)) from exc

    try:
        result = await asyncio.wait_for(_invoke_handler(), timeout=handler_timeout(row.get("type")))
    except _HandlerRaisedTimeout as wrapped:
        cause = wrapped.__cause__
        await _requeue_or_fail(
            action_id,
            f"handler timeout: {cause}" if str(cause or "") else "handler timeout",
            cause,
        )
        return
    except TimeoutError:
        # asyncio cancels the awaiter, but it cannot stop synchronous work
        # already dispatched through asyncio.to_thread. Requeueing here could
        # start a second copy while the first still commits. Preserve the
        # uncertainty as a terminal state for explicit reconciliation.
        queue.transition_status(
            action_id,
            "executing",
            "ambiguous",
            result=(
                f"executor deadline exceeded after {handler_timeout(row.get('type')):g}s; "
                "handler outcome is unknown and was not retried"
            ),
        )
        return
    except Exception as e:  # noqa: BLE001
        log.exception("handler %s failed for action %s", type_str, action_id)
        await _requeue_or_fail(action_id, f"exception: {e}", e)
        return

    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        queue.transition_status(
            action_id,
            "executing",
            "failed",
            result="invalid handler result: expected {'ok': bool, 'result': str}",
        )
        return

    result_text = str(result.get("result", ""))
    if result.get("ok"):
        if result.get("partial"):
            queue.transition_status(action_id, "executing", "partial", result=result_text)
        else:
            queue.transition_status(action_id, "executing", "executed", result=result_text)
    elif result.get("ambiguous"):
        queue.transition_status(
            action_id,
            "executing",
            "ambiguous",
            result=result_text or "handler outcome is unknown and was not retried",
        )
    elif result.get("retryable"):
        await _requeue_or_fail(
            action_id,
            result_text or "handler returned retryable failure",
            TransientActionError(result_text),
        )
    else:
        queue.transition_status(
            action_id,
            "executing",
            "failed",
            result=result_text or "handler returned ok=False",
        )


async def drain_once() -> int:
    from src.brain.actions.expiry import sweep_expired, sweep_stuck_executing
    sweep_expired()
    sweep_stuck_executing()
    rows = queue.list_by_status("approved")
    for row in rows:
        await _run_one(row)
    return len(rows)


async def run_forever() -> None:
    log.info("action executor starting (poll=%.1fs, timeout=%.1fs)", POLL_INTERVAL_SECS, HANDLER_TIMEOUT_SECS)
    while True:
        try:
            n = await drain_once()
            if n:
                log.info("executor drained %d action(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("executor drain loop error")
        await asyncio.sleep(POLL_INTERVAL_SECS)
