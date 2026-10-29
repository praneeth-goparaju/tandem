from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from src.brain.actions import queue
from src.brain.actions.approval_review import (
    build_approval_review,
    payload_validation_error,
)
from src.brain.actions.handlers.insert_vault_line import (
    insert_after_anchor,
    validate_single_line,
)
from src.brain.actions.outcomes import is_transient_exception
from src.brain.actions.types import (
    AUTO_ELIGIBLE_TYPES,
    MAX_VAULT_FILE_CHANGES,
    OWN_DEVICE_AUTO_TYPES,
    REMINDERS_ACCESS_TYPES,
    TRUSTED_CLIENT_ACTION_TYPES,
    TRUSTED_CLIENT_APPROVAL_CHANNEL,
    Action,
    ActionType,
    Risk,
    validate_vault_note_path,
)
from src.brain.atomic_io import confined_path, read_regular_text
from src.brain.mcp._common import _bracket, _err, _ok, _wrap_external, notifier
from src.brain.mcp.caller_context import ToolCaller, require_current_caller
from src.brain.mcp.tool_registry import brain_tool
from src.brain.runtime.vault_paths import path_in_vault_paths
from src.db import short_id

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]

_VALID_TYPES = {t.value for t in ActionType}
_VALID_RISKS = {r.value for r in Risk}
_ORIGIN_ROUTED_TYPES = {
    ActionType.PUSH_NOTIFICATION.value,
    ActionType.SEND_DRAFT.value,
    ActionType.PROMOTE_NOTE.value,
}
_MAX_PENDING_LIFETIME = timedelta(days=7)
_SERVICE_WAIT_SECS = 30.0
_SERVICE_POLL_SECS = 0.5
_REVIEW_DELIVERY_NAMESPACE = uuid.UUID("cc006621-0546-57cf-b0ee-33cb58b721fe")


def _review_note(delivery_error: str | None) -> str:
    if delivery_error:
        return "; approval review could not be delivered"
    return "; approval review delivered to Slack"


async def _ensure_scheduled_review(action_id: str, area: str, agent: str) -> str | None:
    row = queue.get(action_id)
    if row is None:
        return "queued action disappeared"
    payload_hash = str(row.get("payload_hash") or "")
    if queue.review_was_delivered(action_id, payload_hash, area):
        return None
    delivery = notifier()
    key = str(uuid.uuid5(_REVIEW_DELIVERY_NAMESPACE, action_id))
    for attempt in range(2):
        try:
            result = await delivery.send(
                "This scheduled action came from untrusted data. Review its exact effect below.",
                title="Approval required",
                area=area,
                agent=agent,
                idempotency_key=key,
                proposal_action_ids=[action_id],
            )
        except Exception as exc:  # noqa: BLE001
            if attempt == 0 and is_transient_exception(exc):
                continue
            log.warning("approval review delivery failed for %s: %s", action_id, exc)
            return "notifier failed"
        if result.get("status") == "sent":
            if queue.review_was_delivered(action_id, payload_hash, area):
                return None
            if attempt == 0:
                continue
            log.warning("approval review delivery for %s had no complete receipt", action_id)
            return "complete review receipt was not recorded"
        if attempt == 0 and result.get("retryable") is True:
            continue
        reason = str(result.get("error") or "notifier did not deliver")
        log.warning("approval review delivery failed for %s: %s", action_id, reason)
        return reason
    return "notifier failed"


def _completed_action_result(action_id: str, row: dict) -> dict:
    status = row["status"]
    result = row.get("result") or ""
    if status == "executed":
        return _ok(f"action {action_id} executed: {result}")
    if status == "partial":
        return _ok(f"action {action_id} partial: {result}")
    if status == "ambiguous":
        return _ok(f"action {action_id} AMBIGUOUS: {result}")
    if status == "approved":
        return _ok(
            f"action {action_id} hit a temporary failure and is queued for retry: "
            f"{result}"
        )
    return _ok(f"action {action_id} FAILED: {result}")


async def _execute_queued_action(
    action_id: str,
    *,
    channel: str,
    actor: str | None = None,
) -> dict:
    from src.brain.actions.executor import _run_one

    changed = queue.transition_status(
        action_id,
        "pending",
        "approved",
        channel=channel,
        actor=actor,
    )
    row = queue.get(action_id)
    if row is None:
        return _ok(f"action {action_id} FAILED: queued action disappeared")
    if not changed:
        return _ok(
            f"action {action_id} already {row['status']} (state changed before execution)"
        )
    if await asyncio.to_thread(_needs_background_service, row["type"]):
        return await _await_background_service(action_id)
    await _run_one(row)
    final = queue.get(action_id)
    if final is None:
        return _ok(
            f"action {action_id} FAILED: queued action disappeared after execution"
        )
    return _completed_action_result(action_id, final)


def _needs_background_service(action_type: str) -> bool:
    if ActionType(action_type) not in REMINDERS_ACCESS_TYPES:
        return False
    from src.brain.mcp.tools import _icloud

    return not _icloud.reminders_access_granted()


async def _await_background_service(action_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + _SERVICE_WAIT_SECS
    while True:
        row = queue.get(action_id)
        if row is None:
            return _ok(
                f"action {action_id} FAILED: queued action disappeared after execution"
            )
        if row["status"] not in {"approved", "executing"}:
            return _completed_action_result(action_id, row)
        if asyncio.get_running_loop().time() >= deadline:
            return _ok(
                f"action {action_id} is approved but has not run yet. This process "
                "has no Reminders access, so the action was left for the background "
                f"service, which did not finish it within {_SERVICE_WAIT_SECS:g} "
                "seconds. It runs as soon as the service picks it up; check that "
                "the service is running."
            )
        await asyncio.sleep(_SERVICE_POLL_SECS)


def _normalize_expiry(value: object, *, now: datetime) -> tuple[str | None, str | None]:
    if value in (None, ""):
        return (now + _MAX_PENDING_LIFETIME).isoformat(timespec="seconds"), None
    if not isinstance(value, str):
        return None, "expires_at must be an ISO8601 timestamp"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "expires_at must be a valid ISO8601 timestamp"
    if parsed.tzinfo is None:
        return None, "expires_at must include a UTC offset"
    expiry = parsed.astimezone(UTC)
    if expiry <= now:
        return None, "expires_at must be in the future"
    if expiry > now + _MAX_PENDING_LIFETIME:
        return None, "expires_at cannot be more than 7 days in the future"
    return expiry.isoformat(timespec="seconds"), None


@lru_cache(maxsize=1)
def _risk_policy() -> dict[str, str]:
    import yaml
    out: dict[str, str] = {}
    for path in (_REPO_ROOT / "config" / "risk_policy.yaml",
                 _REPO_ROOT / "user" / "risk_policy.yaml"):
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                for atype, risk in (data.get("defaults") or {}).items():
                    if risk in _VALID_RISKS:
                        out[atype] = risk
                    else:
                        log.warning("risk_policy.yaml: %s has invalid risk %r (valid: %s)",
                                    atype, risk, sorted(_VALID_RISKS))
            except yaml.YAMLError as e:
                log.warning("risk_policy.yaml parse error in %s: %s", path, e)
    return out


def _snapshot_prerequisite_error(type_: str, payload: dict) -> str | None:
    if type_ == "edit_vault_files":
        changes = payload.get("changes")
        if not isinstance(changes, list) or not changes:
            return (
                "edit_vault_files payload requires a non-empty 'changes' list; "
                "each item needs path and the COMPLETE new_content. "
                "The unsupported 'edits'/'operation'/'content' append shape must not be used."
            )
        if len(changes) > MAX_VAULT_FILE_CHANGES:
            return (
                "edit_vault_files payload has too many changes "
                f"({len(changes)} > {MAX_VAULT_FILE_CHANGES}); split it by topic"
            )
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                return f"edit_vault_files change {index} must be an object"
            if set(change) - {
                "path", "new_content", "preserve_sections", "expected_sha256",
            }:
                return f"edit_vault_files change {index} has unsupported fields"
            try:
                validate_vault_note_path(change.get("path"))
            except ValueError as exc:
                return f"edit_vault_files change {index}: {exc}"
            if not isinstance(change.get("new_content"), str):
                return f"edit_vault_files change {index} requires COMPLETE new_content"
    elif type_ == "edit_vault_file":
        if set(payload) - {
            "path", "new_content", "preserve_sections", "expected_sha256",
        }:
            return "edit_vault_file payload has unsupported fields"
        try:
            validate_vault_note_path(payload.get("path"))
        except ValueError as exc:
            return f"edit_vault_file payload: {exc}"
        if not isinstance(payload.get("new_content"), str):
            return "edit_vault_file payload requires COMPLETE new_content"
    elif type_ == "insert_vault_line":
        if set(payload) - {"path", "anchor", "text"}:
            return (
                "insert_vault_line payload has unsupported fields; "
                "it takes exactly path, anchor and text"
            )
        try:
            validate_vault_note_path(payload.get("path"))
            validate_single_line(payload.get("anchor"), "anchor")
            validate_single_line(payload.get("text"), "text")
        except ValueError as exc:
            return f"insert_vault_line payload: {exc}"
    elif type_ == "send_draft":
        if not str(payload.get("draft_path") or "").strip():
            return "send_draft payload requires draft_path"
    return None


_VAULT_EDIT_TYPES = frozenset({"edit_vault_file", "edit_vault_files", "insert_vault_line"})


def _place_path_error(type_: str, payload: dict, caller: ToolCaller) -> str | None:
    if caller.areas is None or type_ not in _VAULT_EDIT_TYPES:
        return None
    paths = (
        [change["path"] for change in payload["changes"]]
        if type_ == "edit_vault_files"
        else [payload["path"]]
    )
    allowed = caller.vault_paths or ()
    if all(path_in_vault_paths(path, allowed) for path in paths):
        return None
    if not allowed:
        return "this chat may not propose vault edits; nothing was read"
    return f"this chat may propose vault edits only inside: {', '.join(allowed)}; nothing was read"


def _verification_run() -> bool:
    from src.brain.mcp.tools.alfred import _verification_run as verification_run

    return verification_run()


def _starting_risk(type_: str, requested_risk: str | None, direct_client: bool) -> tuple[str, str | None]:
    if (
        requested_risk is not None
        and not direct_client
        and ActionType(type_) in OWN_DEVICE_AUTO_TYPES
    ):
        log.info(
            "propose_action: ignoring caller risk=%r for own-device type %r; "
            "the central policy decides",
            requested_risk,
            type_,
        )
        requested_risk = None
    risk = (
        Risk.AUTO.value
        if direct_client
        else requested_risk or _risk_policy().get(type_, Risk.CONFIRM.value)
    )
    return risk, requested_risk


def _bind_provenance(type_: str, payload: dict, origin_agent: str, origin_area: str) -> None:
    if type_ in _ORIGIN_ROUTED_TYPES or "area" in payload:
        payload["area"] = origin_area
    if type_ in _ORIGIN_ROUTED_TYPES or "agent" in payload:
        payload["agent"] = origin_agent
    if "proposing_agent" in payload:
        payload["proposing_agent"] = origin_agent


def _snapshot_vault_edit(type_: str, payload: dict) -> tuple[dict, str | None]:
    from src.brain.actions.handlers.edit_vault_file import (
        _read_existing,
        merge_final_content,
    )
    from src.orchestrator.config import VAULT_PATH

    def freeze(change: dict) -> dict:
        frozen = dict(change)
        target = confined_path(VAULT_PATH, validate_vault_note_path(change["path"]))
        existing = _read_existing(target)
        frozen["expected_sha256"] = (
            hashlib.sha256(existing.encode()).hexdigest()
            if existing is not None else None
        )
        frozen["new_content"] = merge_final_content(
            existing, change["new_content"], change.get("preserve_sections")
        )
        frozen.pop("preserve_sections", None)
        return frozen

    try:
        if type_ == "edit_vault_file":
            payload = freeze(payload)
        else:
            payload["changes"] = [freeze(change) for change in payload["changes"]]
    except (OSError, ValueError) as exc:
        return payload, f"cannot snapshot vault note for approval: {exc}"
    return payload, None


async def _check_vault_line(payload: dict) -> str | None:
    from src.orchestrator.config import VAULT_PATH

    rel = payload["path"]
    try:
        existing = await asyncio.to_thread(
            read_regular_text, confined_path(VAULT_PATH, rel)
        )
        insertion = insert_after_anchor(
            existing, payload["anchor"], payload["text"]
        )
    except FileNotFoundError:
        return f"insert_vault_line: {rel} does not exist"
    except (OSError, ValueError) as exc:
        return f"insert_vault_line: {exc}"
    if insertion.already_present:
        return "insert_vault_line: text is already present directly after the anchor"
    return None


def _snapshot_intake(payload: dict) -> str | None:
    from src.brain.actions.handlers.promote_intake import intake_snapshot
    from src.db import get_db

    row = get_db().execute(
        "SELECT content, source, source_id FROM intake WHERE id=? AND status='pending'",
        (str(payload.get("intake_id") or "").strip(),),
    ).fetchone()
    if row is None:
        return "intake row is missing or no longer pending"
    payload.update(intake_snapshot(*map(str, row)))
    return None


async def _snapshot_mail(payload: dict) -> str | None:
    from src.brain.mcp.tools.mail_ops import snapshot_message

    provider = str(payload.get("provider") or "").strip()
    uid = str(payload.get("uid") or "").strip()
    mailbox = str(payload.get("mailbox") or "INBOX").strip()
    try:
        snapshot = await snapshot_message(provider, uid, mailbox)
    except Exception as exc:  # noqa: BLE001
        return f"cannot snapshot mail for approval: {exc}"
    payload.update({"provider": provider, "uid": uid, "mailbox": mailbox})
    payload.update(snapshot)
    return None


async def _snapshot_mail_batch(payload: dict) -> tuple[dict, str | None]:
    from src.brain.actions.approval_review import MAX_ARCHIVE_BATCH

    uids = payload.get("uids")
    if not isinstance(uids, list) or not 1 <= len(uids) <= MAX_ARCHIVE_BATCH:
        return payload, f"mail_archive_batch needs uids: a list of 1 to {MAX_ARCHIVE_BATCH} message uids"
    messages = []
    for uid in dict.fromkeys(str(uid).strip() for uid in uids):
        message = {"provider": payload.get("provider"), "uid": uid, "mailbox": payload.get("mailbox") or "INBOX"}
        error = await _snapshot_mail(message)
        if error:
            return payload, f"uid {uid}: {error}; nothing was proposed"
        messages.append(message)
    return {"summary": str(payload.get("summary") or "").strip(), "messages": messages}, None


async def _snapshot_draft(payload: dict) -> str | None:
    from src.brain.actions.handlers._draft_io import snapshot_draft
    from src.orchestrator.config import VAULT_PATH

    try:
        draft_path, snapshot = await asyncio.to_thread(
            snapshot_draft, VAULT_PATH, payload.get("draft_path")
        )
    except (OSError, PermissionError, ValueError) as exc:
        return f"cannot snapshot draft for approval: {exc}"
    payload["draft_path"] = draft_path
    payload.update(snapshot)
    return None


async def _snapshot_for_approval(type_: str, payload: dict) -> tuple[dict, str | None]:
    if type_ in {"edit_vault_file", "edit_vault_files"}:
        return _snapshot_vault_edit(type_, payload)
    if type_ == "insert_vault_line":
        return payload, await _check_vault_line(payload)
    if type_ == "promote_intake":
        return payload, _snapshot_intake(payload)
    if type_ == "mail_archive":
        return payload, await _snapshot_mail(payload)
    if type_ == "mail_archive_batch":
        return await _snapshot_mail_batch(payload)
    if type_ == "send_draft":
        return payload, await _snapshot_draft(payload)
    return payload, None


def _occurrence_key(raw: object) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        return None, "occurrence_key must be a non-empty string when provided"
    key = raw.strip()
    if len(key) > 200:
        return None, "occurrence_key must be at most 200 characters"
    return key, None


async def _involves_others(type_: str, payload: dict) -> bool:
    if type_ == "create_calendar_event":
        return bool(payload.get("attendees"))
    if type_ == "update_calendar_event":
        from src.brain.actions.handlers.update_calendar_event import event_involves_others

        return await event_involves_others(str(payload.get("event_url") or ""))
    return False


def _final_risk(
    type_: str,
    risk: str,
    *,
    requested_risk: str | None,
    raw_risk: object,
    payload: dict,
    caller: ToolCaller,
    direct_client: bool,
    fixed_route_requested: bool,
    involves_others: bool = False,
) -> str:
    calendar = type_ in ("create_calendar_event", "update_calendar_event")
    if calendar and requested_risk is None and risk == "confirm" and not involves_others:
        risk = "auto"

    if (
        risk == "auto"
        and ActionType(type_) not in AUTO_ELIGIBLE_TYPES
        and not direct_client
    ):
        log.warning(
            "propose_action: clamped risk=auto->confirm for non-auto-eligible "
            "type %r (caller passed risk=%r)", type_, raw_risk,
        )
        risk = "confirm"

    if calendar and risk == "auto" and involves_others:
        log.warning("propose_action: clamped calendar event with attendees auto->confirm")
        risk = "confirm"

    if type_ == "create_todoist_task" and payload.get("action_note_id"):
        if risk != "confirm":
            log.warning("propose_action: linked action-note task forced to confirm")
        risk = "confirm"

    fixed_route_notification = (
        fixed_route_requested
        and type_ == ActionType.PUSH_NOTIFICATION.value
        and not caller.is_reply
        and not any(
            field in payload
            for field in ("occasion_person_id", "occasion", "occasion_year")
        )
    )
    own_device_auto = ActionType(type_) in OWN_DEVICE_AUTO_TYPES and not involves_others
    if (
        caller.untrusted_input
        and risk == "auto"
        and not fixed_route_notification
        and not own_device_auto
    ):
        log.warning("propose_action: untrusted context forced auto->confirm")
        risk = "confirm"

    if type_ == ActionType.INSERT_VAULT_LINE.value:
        risk = Risk.CONFIRM.value
    if _verification_run():
        risk = Risk.CONFIRM.value
    return risk


async def _queue_and_run(
    action: Action, caller: ToolCaller, *, origin_agent: str, origin_area: str, direct_client: bool,
) -> dict:
    risk, type_ = action.risk.value, action.type.value
    direct_review = (
        Risk(risk) is Risk.CONFIRM
        and caller.untrusted_input
        and not caller.is_reply
    )
    try:
        queue.insert(action)
    except queue.DuplicateActionError as e:
        existing = e.existing_id or "unknown"
        existing_status = e.existing_status or "unknown"
        if direct_client and existing_status == "pending" and e.existing_id:
            return await _execute_queued_action(
                e.existing_id,
                channel=TRUSTED_CLIENT_APPROVAL_CHANNEL,
                actor=origin_agent,
            )
        if existing_status == "pending":
            duplicate_state = "already pending"
        else:
            duplicate_state = f"already {existing_status}"
        review_note = ""
        if direct_review and existing_status == "pending" and e.existing_id:
            delivery_error = await _ensure_scheduled_review(
                e.existing_id, origin_area, origin_agent
            )
            review_note = _review_note(delivery_error)
        return _ok(
            f"action {existing} {duplicate_state} (database dedup match) — skipped"
            f"{review_note}"
        )

    review_note = ""
    if direct_review:
        delivery_error = await _ensure_scheduled_review(
            action.id, origin_area, origin_agent
        )
        review_note = _review_note(delivery_error)

    if Risk(risk) is Risk.AUTO:
        return await _execute_queued_action(
            action.id,
            channel=(
                TRUSTED_CLIENT_APPROVAL_CHANNEL if direct_client else "auto"
            ),
            actor=origin_agent if direct_client else None,
        )

    return _ok(
        f"action {action.id} queued (type={type_}, risk={risk}){review_note}"
    )


@brain_tool(
    name="propose_action",
    description=(
        "Propose a world-effect (send a draft, push a notification, edit a vault file, "
        "etc.) for the executor to run after approval. This is the ONLY way to put work "
        "into the action queue — do not call notifier, vault, or task backends directly. "
        "Keep rationale to one concise sentence. Omit risk to use the centrally configured "
        "policy. Reminders (adding and completing), task-list additions, and adding or "
        "updating calendar events without attendees always follow the policy and run "
        "without approval; a risk value passed for them is ignored. "
        "Use risk='confirm' only when the action meaningfully affects another person, shared "
        "state, sensitive knowledge, money, or something difficult to undo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": f"One of: {sorted(_VALID_TYPES)}"},
            "payload": {"type": "object", "description": "Type-specific payload (see handler docs)."},
            "rationale": {"type": "string", "description": "One concise sentence explaining what will happen and why now. Mandatory."},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}, "description": "Finding ids that triggered this action (for traceability)."},
            "risk": {"type": "string", "enum": sorted(_VALID_RISKS), "description": "Optional override. Omit to use the centrally configured policy."},
            "occurrence_key": {
                "type": "string",
                "description": (
                    "Stable business occurrence id for an intentional repeat of an otherwise "
                    "identical action (for example daily-brief:2026-08-23). Retries of the same "
                    "occurrence must reuse the same key."
                ),
            },
            "expires_at": {
                "type": "string",
                "description": (
                    "ISO8601 timestamp after which a pending action expires; must be "
                    "in the future and no more than 7 days away."
                ),
            },
        },
        "required": ["type", "payload", "rationale"],
    },
)
async def propose_action(args, *, _fixed_route_notification: bool = False):
    type_ = args.get("type", "")
    if type_ not in _VALID_TYPES:
        return _err(f"unknown action type '{type_}'. Valid: {sorted(_VALID_TYPES)}")

    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    if type_ not in caller.allowed_action_types:
        return _err(
            f"action type '{type_}' is not allowed for agent '{caller.agent}'"
        )
    origin_agent = caller.agent
    origin_area = caller.area or caller.agent
    direct_client = (
        ActionType(type_) in TRUSTED_CLIENT_ACTION_TYPES
        and type_ in caller.direct_action_types
        and not caller.untrusted_input
    )

    rationale = (args.get("rationale") or "").strip()
    if not rationale:
        return _err("rationale is required and must be non-empty")

    requested_risk = args.get("risk")
    if requested_risk is not None and requested_risk not in _VALID_RISKS:
        return _err(
            f"unknown risk '{requested_risk}'. Valid: {sorted(_VALID_RISKS)}"
        )
    risk, requested_risk = _starting_risk(type_, requested_risk, direct_client)

    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        return _err("payload must be an object")
    payload = dict(payload)
    _bind_provenance(type_, payload, origin_agent, origin_area)
    payload_error = _snapshot_prerequisite_error(type_, payload) or _place_path_error(
        type_, payload, caller,
    )
    if payload_error:
        return _err(payload_error)

    payload, snapshot_error = await _snapshot_for_approval(type_, payload)
    if snapshot_error:
        return _err(snapshot_error)

    payload_error = payload_validation_error(ActionType(type_), payload)
    if payload_error:
        return _err(payload_error)

    occurrence_key, key_error = _occurrence_key(args.get("occurrence_key"))
    if key_error:
        return _err(key_error)

    risk = _final_risk(
        type_, risk,
        requested_risk=requested_risk, raw_risk=args.get("risk"), payload=payload,
        caller=caller, direct_client=direct_client,
        fixed_route_requested=_fixed_route_notification,
        involves_others=await _involves_others(type_, payload),
    )

    approval_audience = f"area:{origin_area}"

    from src.brain.actions.expiry import sweep_expired

    sweep_expired()

    expires_at, expiry_error = _normalize_expiry(
        args.get("expires_at"), now=datetime.now(UTC)
    )
    if expiry_error:
        return _err(expiry_error)

    action = Action(
        id=short_id(),
        type=ActionType(type_),
        payload=payload,
        rationale=rationale,
        source_finding_ids=list(args.get("source_finding_ids") or []),
        risk=Risk(risk),
        origin_agent=origin_agent,
        origin_area=origin_area,
        approval_audience=approval_audience,
        occurrence_key=occurrence_key,
        expires_at=expires_at,
    )
    return await _queue_and_run(
        action, caller,
        origin_agent=origin_agent, origin_area=origin_area, direct_client=direct_client,
    )


def _sees_every_area(caller: ToolCaller) -> bool:
    from src.orchestrator.registry import AGENT_REGISTRY

    return (AGENT_REGISTRY.get(caller.agent) or {}).get("chat") == "private"


@brain_tool(
    name="list_pending_actions",
    description=(
        "List actions currently in the queue, one entry each with its id, the area "
        "that proposed it, its type and its risk. Use status='pending' to see what's "
        "awaiting approval, 'approved' to see what's about to run, 'executed' or "
        "'failed' for recent history. The council uses this to dedupe before "
        "proposing new actions. A run sees its own area's actions, a chat in the "
        "shared channel the actions of the shared areas, and a chat in the direct "
        "messages and the closing run every area's."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "expired", "executed", "partial", "ambiguous", "failed"], "description": "Filter by status. Default: pending."},
            "limit": {"type": "integer", "description": "Max rows. Default: 50."},
        },
    },
)
async def list_pending_actions(args):
    status = args.get("status", "pending")
    if status not in {
        "pending", "approved", "rejected", "expired", "executed", "partial",
        "ambiguous", "failed",
    }:
        return _err("invalid action status")
    try:
        limit = max(1, min(int(args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        return _err("limit must be an integer")
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    if caller.areas is not None:
        rows = queue.list_by_status(status, limit=limit, origin_areas=sorted(caller.areas))
    elif _sees_every_area(caller):
        rows = queue.list_by_status(status, limit=limit)
    else:
        rows = queue.list_by_status(
            status,
            limit=limit,
            origin_agent=caller.agent,
            origin_area=caller.area or caller.agent,
        )
    if not rows:
        return _ok(f"No actions in status '{status}'.")

    lines: list[str] = []
    for r in rows:
        lines.append(
            f"id={_bracket(r['id'])} | area={_bracket(r.get('origin_area'))} "
            f"| type={_bracket(r['type'])} | risk={_bracket(r['risk'])}\n"
            + _wrap_external(
                str(r.get("rationale") or "")[:120],
                source="pending-action/rationale",
            )
        )
        review = build_approval_review(r)
        effect = "\n".join(review.summary_lines) if review.approvable else review.reason
        if effect:
            lines.append(_wrap_external(effect, source="pending-action/effect"))
    return _ok("\n".join(lines))


