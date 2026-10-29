from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from src.brain.actions.types import ActionType
from src.brain.mcp._common import _bracket
from src.brain.mcp.tool_registry import brain_tool
from src.brain.plugins.base import DELIVERY_SUCCESS_STATES

_log = logging.getLogger(__name__)

# iOS Mail.app silently truncates / drops `mailto:` bodies once the
# assembled URL exceeds ~2000-2500 chars. Cap the parent quote so the
# final URL stays well under that limit; users wanting the full original
# tap through to the parent thread.
QUOTE_BUDGET = 900


def _payload(args: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {k: args[k] for k in keys if k in args and args[k] is not None}


async def _propose(
    action_type: ActionType,
    payload: dict[str, Any],
    args: dict[str, Any],
    *,
    forced_risk: str | None = None,
) -> dict[str, Any]:
    from src.brain.mcp.tools.actions import propose_action

    return await propose_action({
        "type": action_type.value,
        "payload": payload,
        "rationale": args["rationale"],
        "source_finding_ids": list(args.get("source_finding_ids") or []),
        "risk": forced_risk if forced_risk is not None else args.get("risk"),
    })


@brain_tool(
    name="create_calendar_event",
    description=(
        "Propose creating a new iCloud calendar event. Default risk: confirm "
        "(needs Slack ✅). Required: summary, start, end (ISO 8601), rationale. Optional: "
        "all_day, location, description, attendees, calendar_name, and "
        "alarm_minutes_before (default 60; 0 fires at the start)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601, e.g. 2026-05-12T14:00:00+02:00"},
            "end": {"type": "string"},
            "all_day": {"type": "boolean"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "calendar_name": {"type": "string"},
            "alarm_minutes_before": {"type": "integer", "minimum": 0, "maximum": 10080},
            "rationale": {"type": "string", "description": "Why this event, why now."},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["summary", "start", "end", "rationale"],
    },
)
async def create_calendar_event(args):
    payload = _payload(args, (
        "summary", "start", "end", "all_day", "location", "description",
        "attendees", "calendar_name", "alarm_minutes_before",
    ))
    return await _propose(ActionType.CREATE_CALENDAR_EVENT, payload, args)


@brain_tool(
    name="update_calendar_event",
    description=(
        "Propose updating an existing iCloud calendar event by its CalDAV URL "
        "(from calendar_list_events). Default risk: confirm. Pass only the "
        "fields you want to change (summary, start, end, location, description, "
        "cancelled). Concurrency-safe: the handler retries on ETag races once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "event_url": {"type": "string"},
            "summary": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "cancelled": {"type": "boolean"},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["event_url", "rationale"],
    },
)
async def update_calendar_event(args):
    payload = _payload(args, (
        "event_url", "summary", "start", "end", "location", "description",
        "cancelled",
    ))
    return await _propose(ActionType.UPDATE_CALENDAR_EVENT, payload, args)


@brain_tool(
    name="mail_reply_link",
    description=(
        "Scaffold a mail reply. Two modes, picked automatically from the "
        "parent message:\n\n"
        "**Non-HMA threads** — returns a `mailto:` URL. Push it to Slack "
        "as `[Compose reply in Mail.app](<url>)`. Tap opens Mail.app "
        "compose with To/Subject/Body/threading filled; Mail.app handles "
        "From, signature, dark mode, quote rendering.\n\n"
        "**HMA threads** (parent arrived at `<random>@icloud.com` or "
        "`<random>@privaterelay.appleid.com`) — mailto can't set From to "
        "an HMA, and HMAs aren't selectable in Mail.app's compose From "
        "dropdown. So this tool itself emits 2 Slack messages: a context "
        "push (where the HMA is, where the parent lives) + a threaded "
        "follow-up containing only your reply body as non-markup plain text, "
        "ready for copy. The user opens Mail.app, "
        "taps Reply on the parent (Apple's relay sets From to the HMA "
        "automatically), and pastes.\n\n"
        "Required: parent_uid (from mail_search), body, rationale. "
        "Optional: additional_to, to_override, cc, bcc, quote_timestamp, "
        "finding_id. HMA Slack routing and persona are taken from the "
        "authenticated caller context; they are not model-selectable."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "parent_uid": {"type": "string"},
            "parent_mailbox": {"type": "string"},
            "body": {"type": "string", "description": "ONLY your new reply text. Non-HMA: parent body is auto-quoted into the mailto. HMA: this body lands verbatim in a plain-text Slack message — write it as if you're pasting into Mail.app."},
            "additional_to": {"type": "array", "items": {"type": "string"}},
            "to_override": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Override To: (e.g. parent says 'don't reply, contact info@gym.example.com'). Non-HMA path only."},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "quote_timestamp": {"type": "string", "description": "Optional Slack ts of the inbound user message that triggered this reply — threads the HMA context push under it instead of starting a new top-level thread."},
            "finding_id": {"type": "string", "description": "Optional finding id for the notification audit row."},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["parent_uid", "body", "rationale"],
    },
)
async def mail_reply_link(args):
    import email.utils as _eu

    from src.brain.mcp._common import notifier
    from src.brain.mcp.tools._icloud import (
        ICloudNotConfigured,
        require_creds,
    )
    from src.brain.mcp.tools._mail_quote import (
        fetch_parent_for_quote,
        pick_hma_recipient,
        quote_lines,
    )
    from src.brain.mcp.tools._mailto import build_mailto
    from src.brain.notifications_audit import record_notification
    from src.orchestrator.config import (
        ICLOUD_APPLE_ID,
        ICLOUD_FROM_ALIASES,
        ICLOUD_IMAP_USER,
    )

    parent_uid = (args.get("parent_uid") or "").strip()
    body = args.get("body") or ""
    if not parent_uid:
        return {"content": [{"type": "text", "text": "ERROR: parent_uid is required"}]}
    if not body.strip():
        return {"content": [{"type": "text", "text": "ERROR: body is required"}]}

    try:
        require_creds()
    except ICloudNotConfigured as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}]}

    parent_mailbox = (args.get("parent_mailbox") or "INBOX").strip()
    parent = fetch_parent_for_quote(parent_mailbox, parent_uid)
    if parent is None or not parent.from_:
        return {"content": [{"type": "text", "text": (
            f"ERROR: parent uid={parent_uid} not fetchable from {parent_mailbox!r}"
        )}]}

    parent_from_addr = _eu.parseaddr(parent.from_)[1] or parent.from_
    parent_subject = parent.subject
    subject = (
        parent_subject if parent_subject.lower().startswith("re:")
        else f"Re: {parent_subject or '(no subject)'}"
    )

    # ── HMA branch ─────────────────────────────────────────────────────
    # Apple's relay rewrites From to the HMA only when the user taps
    # Mail.app's native Reply button on the stored parent. mailto: +
    # In-Reply-To bypasses that binding. Push text-only instead so the
    # user goes through native Reply and the HMA From is preserved.
    # Owned aliases include ICLOUD_APPLE_ID and ICLOUD_IMAP_USER on top of
    # ICLOUD_FROM_ALIASES — without them, a misconfigured FROM_ALIASES
    # would misclassify every reply to the user's primary as HMA.
    owned_aliases = [
        a for a in (*ICLOUD_FROM_ALIASES, ICLOUD_APPLE_ID, ICLOUD_IMAP_USER) if a
    ]
    hma = pick_hma_recipient(parent, owned_aliases)
    if hma:
        from src.brain.mcp.caller_context import require_current_caller

        try:
            caller = require_current_caller()
        except PermissionError as exc:
            return {"content": [{"type": "text", "text": (
                f"ERROR: parent arrived at HMA {_bracket(hma)}, but {exc}"
            )}]}
        area = caller.area or caller.agent
        agent = caller.agent

        finding_id = (args.get("finding_id") or "").strip() or None
        if not finding_id:
            sf = args.get("source_finding_ids") or []
            finding_id = (str(sf[0]).strip() if sf else "") or None
        quote_ts = (args.get("quote_timestamp") or "").strip() or None

        ctx_text = (
            "📧 Reply on HMA thread\n"
            f"Arrived at: {_bracket(hma)}\n"
            f"From: {_bracket(parent.from_)}\n"
            f"Subject: {_bracket(subject)}\n"
            f"Mailbox: {_bracket(parent_mailbox)}\n"
            f"UID: {_bracket(parent_uid)}\n\n"
            "⚠️ Open Mail.app on this thread → tap Reply "
            f"(Mail.app auto-sets From to the HMA).\n"
            "Reply text in 👇"
        )
        bare_text = body.rstrip()

        n = notifier()
        try:
            res1 = await n.send(
                ctx_text, title="", urgency="info",
                finding_id=finding_id, area=area, agent=agent,
                quote_timestamp=quote_ts,
                plain_text=True,
            )
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": (
                f"ERROR: HMA context push failed: {type(e).__name__}: {e}"
            )}]}
        ctx_status = res1.get("status", "unknown")
        ctx_ts = res1.get("id")
        record_notification(
            agent=agent, area=area, title="(mail-reply ctx)",
            message=ctx_text, status=ctx_status, signal_ts=ctx_ts,
            finding_id=finding_id, group_id=res1.get("group_id"),
        )
        if ctx_status not in DELIVERY_SUCCESS_STATES or not ctx_ts:
            return {"content": [{"type": "text", "text": (
                f"ERROR: HMA context push did not succeed "
                f"(status={ctx_status}, ts={ctx_ts}). "
                f"Cannot thread the bare reply."
            )}]}

        try:
            res2 = await n.send(
                bare_text, title="", urgency="info",
                finding_id=finding_id, area=area, agent=agent,
                quote_timestamp=str(ctx_ts),
                plain_text=True,
            )
        except Exception as e:  # noqa: BLE001
            warn_text = f"⚠️ reply body push failed: {type(e).__name__}: {e}"
            try:
                await n.send(
                    warn_text, title="", urgency="info",
                    finding_id=finding_id, area=area, agent=agent,
                    quote_timestamp=str(ctx_ts),
                )
            except Exception as e2:  # noqa: BLE001
                _log.warning(
                    "HMA failure-warning push also failed: %s: %s",
                    type(e2).__name__, e2,
                )
            record_notification(
                agent=agent, area=area, title="(mail-reply body)",
                message=bare_text, status="fail", signal_ts=None,
                finding_id=finding_id, group_id=None,
            )
            return {"content": [{"type": "text", "text": (
                f"ERROR: HMA bare-reply push failed (ctx ts={ctx_ts}): "
                f"{type(e).__name__}: {e}. Posted warning in-thread."
            )}]}
        rep_status = res2.get("status", "unknown")
        rep_ts = res2.get("id")
        record_notification(
            agent=agent, area=area, title="(mail-reply body)",
            message=bare_text, status=rep_status, signal_ts=rep_ts,
            finding_id=finding_id, group_id=res2.get("group_id"),
        )

        if rep_status not in DELIVERY_SUCCESS_STATES or not rep_ts:
            warning = (
                "⚠️ reply body was not delivered; use the prepared text from "
                "the agent response or retry only the body."
            )
            try:
                await n.send(
                    warning, title="", urgency="info",
                    finding_id=finding_id, area=area, agent=agent,
                    quote_timestamp=str(ctx_ts),
                )
            except Exception as warning_error:  # noqa: BLE001
                _log.warning(
                    "HMA failure-warning push also failed: %s: %s",
                    type(warning_error).__name__, warning_error,
                )
            return {"content": [{"type": "text", "text": (
                f"ERROR: HMA reply body was not delivered "
                f"(ctx ts={ctx_ts}, status={rep_status}, reply ts={rep_ts}). "
                "The context header succeeded; retry only the body."
            )}]}

        return {"content": [{"type": "text", "text": (
            f"HMA reply pushed: HMA={_bracket(hma)}, ctx ts={ctx_ts} ({ctx_status}), "
            f"reply ts={rep_ts} ({rep_status}). User must open Mail.app and "
            f"tap Reply on UID {_bracket(parent_uid)} "
            f"({_bracket(parent_mailbox)}); Mail.app "
            f"sets From to the HMA automatically."
        )}]}

    attribution = (
        f"On {parent.date}, {parent.from_} wrote:"
        if (parent.date or parent.from_) else "Original message:"
    )
    parent_text = parent.body
    truncated = parent_text[:QUOTE_BUDGET]
    if len(parent_text) > QUOTE_BUDGET:
        truncated += "\n…[truncated — open the parent thread in Mail.app for the full message]"
    final_body = f"{body.rstrip()}\n\n{attribution}\n{quote_lines(truncated, max_chars=QUOTE_BUDGET + 200)}\n"

    if args.get("to_override"):
        ov = args["to_override"]
        to_list = ov if isinstance(ov, list) else [ov]
    else:
        to_list = [parent_from_addr] + list(args.get("additional_to") or [])

    msgid = parent.msgid
    refs = parent.references
    references_value = (refs + (" " + msgid if refs and msgid else msgid)).strip()

    try:
        url = build_mailto(
            to=to_list,
            subject=subject,
            body=final_body,
            cc=args.get("cc"),
            bcc=args.get("bcc"),
            in_reply_to=msgid or None,
            references=references_value or None,
        )
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}]}

    return {"content": [{"type": "text", "text": (
        f"mailto link ready (length={len(url)}). Push to user as:\n"
        f"`[Compose reply in Mail.app]({url})`\n\n"
        f"Or paste-as-link: {url}"
    )}]}


@brain_tool(
    name="create_reminder",
    description=(
        "Propose creating an Apple Reminders entry. Default risk: auto "
        "(reminders are local + free + one tap to delete). Required: title "
        "and due (ISO 8601 or YYYY-MM-DD). An absolute alarm is attached and "
        "read back. Optional: alarm_minutes_before (default 0), list_name, "
        "notes, priority (0-9; 1=high, 5=med, 9=low, 0=none)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "list_name": {"type": "string"},
            "due": {"type": "string"},
            "alarm_minutes_before": {"type": "integer", "minimum": 0, "maximum": 10080},
            "notes": {"type": "string"},
            "priority": {"type": "integer"},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["title", "due", "rationale"],
    },
)
async def create_reminder(args):
    payload = _payload(args, (
        "title", "list_name", "due", "alarm_minutes_before", "notes", "priority",
    ))
    return await _propose(ActionType.CREATE_REMINDER, payload, args)


@brain_tool(
    name="complete_reminder",
    description=(
        "Propose marking one Apple Reminder complete by its identifier from "
        "reminders_list. A trusted local interactive MCP client executes this "
        "immediately through the audited action queue; other callers default "
        "to confirmation. The handler saves the change through EventKit and "
        "reads it back before reporting success."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Reminder identifier from reminders_list."},
            "rationale": {"type": "string", "description": "Why this reminder is complete."},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["id", "rationale"],
    },
)
async def complete_reminder(args):
    return await _propose(ActionType.COMPLETE_REMINDER, _payload(args, ("id",)), args)


@brain_tool(
    name="create_contact",
    description=(
        "Propose creating a new iCloud contact. Default risk: confirm — contact "
        "edits propagate across every Apple device. Required: full_name, "
        "rationale. Optional: family_name, given_name, emails (list of strings "
        "or {value, type}), tels, org, title, note."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "full_name": {"type": "string"},
            "family_name": {"type": "string"},
            "given_name": {"type": "string"},
            "emails": {"type": "array"},
            "tels": {"type": "array"},
            "org": {"type": "string"},
            "title": {"type": "string"},
            "note": {"type": "string"},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["full_name", "rationale"],
    },
)
async def create_contact(args):
    payload = _payload(args, (
        "full_name", "family_name", "given_name", "emails", "tels",
        "org", "title", "note",
    ))
    return await _propose(ActionType.CREATE_CONTACT, payload, args)


@brain_tool(
    name="update_contact",
    description=(
        "Propose updating an existing iCloud contact by its CardDAV href "
        "(from contacts_search). Default risk: confirm. Pass only the fields "
        "to change. Email/tel arrays come in two flavors: `add_*` appends, "
        "`set_*` replaces. `note_append` adds a line; `note` replaces."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "href": {"type": "string"},
            "full_name": {"type": "string"},
            "family_name": {"type": "string"},
            "given_name": {"type": "string"},
            "add_emails": {"type": "array", "items": {"type": "string"}},
            "set_emails": {"type": "array", "items": {"type": "string"}},
            "add_tels": {"type": "array", "items": {"type": "string"}},
            "set_tels": {"type": "array", "items": {"type": "string"}},
            "org": {"type": "string"},
            "title": {"type": "string"},
            "note": {"type": "string"},
            "note_append": {"type": "string"},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
        },
        "required": ["href", "rationale"],
    },
)
async def update_contact(args):
    payload = _payload(args, (
        "href", "full_name", "family_name", "given_name",
        "add_emails", "set_emails", "add_tels", "set_tels",
        "org", "title", "note", "note_append",
    ))
    return await _propose(ActionType.UPDATE_CONTACT, payload, args)
