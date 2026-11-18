"""The Apple integrations of the vault connector: Reminders, Calendar, Mail and Contacts.

The writes call the action handlers and ``mail_ops.archive_message`` directly:
the owner's Approve tap in claude.ai is the approval, so nothing goes through
the daemon's action queue.

A person whose settings name ``reminder_lists`` or ``calendars`` may use only
those, compared without regard to case or outer spaces. A reminder or event
named by id is checked by the list or calendar it is in. The brain tools match
list and calendar names as text inside the name, so their answers are cut back
to the lines of the allowed names exactly.

Mail, calendar and contacts work on the iCloud account of the person who
calls. For a person with ``icloud_apple_id`` the bridge's process gets that
Apple ID, their mailbox address and an empty ``ICLOUD_APP_PASSWORD`` (empty,
not missing, so ``.env`` cannot fill it in), so the only password it can use is
the one in the login Keychain under that Apple ID; the handlers that run in
the connector get the Apple ID directly. Reminders go through the Apple account
of this Mac for everyone.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import date, datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from src.brain.actions.handlers import (
    complete_reminder,
    create_calendar_event,
    create_reminder,
    update_calendar_event,
    update_reminder,
)
from src.brain.connector.config import ConnectorConfig
from src.brain.connector.tools import (
    Call,
    ICloudCall,
    ToolDef,
    ToolEnv,
    ToolFunc,
    object_schema,
    string_schema,
)
from src.brain.mcp.tool_registry import all_tools
from src.brain.mcp.tools import _icloud as _icloud_creds
from src.brain.mcp.tools import icloud_read as _icloud_read  # noqa: F401  (registers the tools)
from src.brain.mcp.tools import mail_ops
from src.gateway.backend import BackendError, BridgeSettings, CommandBackend
from src.gateway.profiles import load_profile

DEFAULT_ALERT_MINUTES = 60
ICLOUD_ONLY: dict[str, Any] = {
    "type": "string",
    "enum": ["icloud"],
    "description": "Mail account: iCloud only.",
}
OTHERS_REFUSED = (
    "ERROR: this event involves other people; the connector never sends notices, change it by hand"
)
_DUE = "Due time, 'YYYY-MM-DDTHH:MM' local time, or a date (09:00 that day)."
_ALERT = {
    "type": "integer",
    "minimum": 0,
    "maximum": 40320,
    "description": "Minutes before the start that the alert rings.",
}


def icloud_bridge(
    name: str, arguments: dict[str, Any], extra_env: Mapping[str, str] | None = None
) -> str:
    """Run the brain tool ``name`` in its own process, as the gateway does, under the read profile.

    ``extra_env`` is added to that process's environment only. Blocks until
    the process ends, so call it from a worker thread.
    """
    backend = CommandBackend(BridgeSettings.from_environment(os.environ))
    profile = load_profile({"BRAIN_MCP_PROFILE": "read"})
    payload = {
        "name": name,
        "arguments": arguments,
        "allowed": [name],
        "caller_context": dict(profile.caller_context),
    }
    if extra_env:
        result = backend.invoke("call", payload, environment=dict(extra_env))
    else:
        result = backend.invoke("call", payload)
    if not isinstance(result, Mapping):
        raise BackendError("backend call result must be a JSON object")
    content = result.get("content")
    blocks = content if isinstance(content, list) else []
    text = "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    if result.get("is_error") and not text.lstrip().startswith("ERROR:"):
        return f"ERROR: {text or f'{name} failed'}"
    return text


def brain_schema(name: str) -> dict[str, Any]:
    """The registered brain tool's input schema, closed to arguments it does not name."""
    spec = next(tool for tool in all_tools() if tool.name == name)
    schema = {**deepcopy(spec.input_schema), "additionalProperties": False}
    if "provider" in schema.get("properties", {}):
        schema["properties"]["provider"] = dict(ICLOUD_ONLY)
    return schema


def _handler_text(outcome: Mapping[str, Any]) -> str:
    text = str(outcome.get("result") or "")
    if outcome.get("ok"):
        return text or "done"
    return f"ERROR: {text or 'the change was not made'}"


def _account_env(call: Call) -> dict[str, str]:
    """The environment that puts a bridge process on the caller's own iCloud account."""
    if call.icloud_apple_id is None:
        return {}
    return {
        "ICLOUD_APPLE_ID": call.icloud_apple_id,
        "ICLOUD_IMAP_USER": call.icloud_imap_user or "",
        "ICLOUD_APP_PASSWORD": "",
    }


def _account(call: Call) -> dict[str, str]:
    """The keyword that gives a handler the caller's own iCloud account, or nothing."""
    return {} if call.icloud_apple_id is None else {"apple_id": call.icloud_apple_id}


def _not_signed_in(call: Call) -> str | None:
    """The refusal for a person whose iCloud account cannot be used on this Mac.

    A person with no account in the settings uses the Mac's own account only at
    the empty prefix. Anyone else is refused, so one person's mail, calendar and
    contacts are never served to another because a setting was left out.
    """
    if call.icloud_apple_id is None:
        if call.default_icloud:
            return None
        return f"ERROR: {call.person_name} has no iCloud account set up for this connector yet"
    if _icloud_creds._keychain_password(call.icloud_apple_id):
        return None
    return f"ERROR: {call.person_name}'s iCloud account is not signed in on this Mac yet"


async def _bridge(env: ToolEnv, call: Call, name: str, args: dict[str, Any]) -> str:
    """Run the brain tool ``name`` on the caller's iCloud account."""
    refused = _not_signed_in(call)
    if refused is not None:
        return refused
    bridge: ICloudCall = env.icloud or icloud_bridge
    extra = _account_env(call)
    if extra:
        return await asyncio.to_thread(bridge, name, args, extra)
    return await asyncio.to_thread(bridge, name, args)


def _folded(name: str) -> str:
    return name.strip().casefold()


def _allowed(name: str, allowed: tuple[str, ...] | None) -> bool:
    """True when ``allowed`` is None or holds ``name``, ignoring case and outer spaces."""
    return allowed is None or _folded(name) in {_folded(item) for item in allowed}


def _as_allowed(name: str, allowed: tuple[str, ...]) -> str:
    """``name`` spelled as in the settings."""
    return next(item for item in allowed if _folded(item) == _folded(name))


def _refused(call: Call, kind: str, allowed: tuple[str, ...]) -> str:
    return f"ERROR: {call.person_name} can use these {kind}: {', '.join(allowed)}"


_REMINDER_LIST = re.compile(r"^\S+ \[([^\]]*)\] \| title=")


def _reminder_lines(text: str, allowed: tuple[str, ...]) -> list[str]:
    """The reminder lines of a ``reminders_list`` answer whose list is in ``allowed``."""
    kept = []
    for line in text.splitlines():
        found = _REMINDER_LIST.match(line)
        if found is not None and _allowed(found.group(1), allowed):
            kept.append(line)
    return kept


def _event_lines(text: str, allowed: tuple[str, ...]) -> list[str]:
    """The event lines of a ``calendar_list_events`` answer whose calendar is in ``allowed``."""
    kept = []
    for line in text.splitlines():
        parts = line.split(" | ")
        if len(parts) >= 3 and _allowed(parts[1], allowed):
            kept.append(line)
    return kept


async def _reminder_list_refused(reminder_id: str, call: Call) -> str | None:
    """The refusal for a reminder outside the person's lists, or None when they may use it."""
    allowed = call.reminder_lists
    if allowed is None:
        return None
    name = await asyncio.to_thread(update_reminder.list_of, reminder_id)
    if name is None:
        return f"ERROR: reminder {reminder_id!r} not found"
    return None if _allowed(name, allowed) else _refused(call, "lists", allowed)


async def reminder_create(args: dict[str, Any], call: Call) -> str:
    payload = dict(args)
    allowed = call.reminder_lists
    if allowed is not None:
        name = str(payload.get("list_name") or "")
        if not _allowed(name, allowed):
            return _refused(call, "lists", allowed)
        payload["list_name"] = _as_allowed(name, allowed)
    return _handler_text(await create_reminder.execute(payload))


async def reminder_update(args: dict[str, Any], call: Call) -> str:
    refused = await _reminder_list_refused(str(args["id"]), call)
    if refused is not None:
        return refused
    return _handler_text(await update_reminder.execute(dict(args)))


async def reminder_complete(args: dict[str, Any], call: Call) -> str:
    refused = await _reminder_list_refused(str(args["id"]), call)
    if refused is not None:
        return refused
    return _handler_text(await complete_reminder.execute(dict(args)))


async def _reminders_list(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    """List reminders; for a person with allowed lists, only in those lists, one call each."""
    bridge: ICloudCall = env.icloud or icloud_bridge
    allowed = call.reminder_lists
    if allowed is None:
        return await asyncio.to_thread(bridge, "reminders_list", dict(args))
    asked = str(args.get("list_name") or "")
    if asked.strip() and not _allowed(asked, allowed):
        return _refused(call, "lists", allowed)
    names = (_as_allowed(asked, allowed),) if asked.strip() else allowed
    lines: list[str] = []
    for name in names:
        text = await asyncio.to_thread(bridge, "reminders_list", {**args, "list_name": name})
        if text.lstrip().startswith("ERROR:"):
            return text
        lines.extend(_reminder_lines(text, (name,)))
    return "\n".join(lines) if lines else "No reminders found."


async def _calendar_list(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    """List events; for a person with allowed calendars, only in those, in start order."""
    allowed = call.calendars
    if allowed is None:
        return await _bridge(env, call, "calendar_list_events", dict(args))
    asked = str(args.get("calendar_name") or "")
    if asked.strip() and not _allowed(asked, allowed):
        return _refused(call, "calendars", allowed)
    names = (_as_allowed(asked, allowed),) if asked.strip() else allowed
    lines: list[str] = []
    for name in names:
        text = await _bridge(env, call, "calendar_list_events", {**args, "calendar_name": name})
        if text.lstrip().startswith("ERROR:"):
            return text
        lines.extend(_event_lines(text, (name,)))
    return "\n".join(sorted(lines)) if lines else "No events found."


def local_time(value: str, zone: ZoneInfo) -> str:
    """``value`` with ``zone``'s offset when it is a date and time without one.

    The calendar handlers read a time without an offset as UTC.
    """
    try:
        date.fromisoformat(value)
    except ValueError:
        pass
    else:
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        return value
    return moment.replace(tzinfo=zone).isoformat()


def _local_times(args: Mapping[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    payload = dict(args)
    for key in ("start", "end"):
        if isinstance(payload.get(key), str):
            payload[key] = local_time(payload[key], zone)
    return payload


async def calendar_create(args: dict[str, Any], zone: ZoneInfo, call: Call) -> str:
    """Create an event; a person with allowed calendars gets the first when none is named."""
    refused = _not_signed_in(call)
    if refused is not None:
        return refused
    payload = {"alert_minutes": DEFAULT_ALERT_MINUTES, **_local_times(args, zone)}
    allowed = call.calendars
    if allowed is not None:
        name = str(payload.get("calendar_name") or "")
        if not name.strip():
            payload["calendar_name"] = allowed[0]
        elif not _allowed(name, allowed):
            return _refused(call, "calendars", allowed)
        else:
            payload["calendar_name"] = _as_allowed(name, allowed)
    return _handler_text(await create_calendar_event.execute(payload, **_account(call)))


async def calendar_update(args: dict[str, Any], zone: ZoneInfo, call: Call) -> str:
    """Update an event, unless it has attendees or an organizer, or is in a calendar the
    person may not use.

    iCloud tells the other people about a change to such an event, and the
    connector never sends anything.
    """
    refused = _not_signed_in(call)
    if refused is not None:
        return refused
    account = _account(call)
    allowed = call.calendars
    if allowed is not None:
        name = await asyncio.to_thread(
            partial(update_calendar_event.calendar_of, args["event_url"], **account)
        )
        if name is None or not _allowed(name, allowed):
            return _refused(call, "calendars", allowed)
    if await update_calendar_event.event_involves_others(args["event_url"], **account):
        return OTHERS_REFUSED
    return _handler_text(
        await update_calendar_event.execute(_local_times(args, zone), **account)
    )


async def mail_archive(args: dict[str, Any], call: Call) -> str:
    provider = args.get("provider") or "icloud"
    mailbox = args.get("mailbox") or "INBOX"
    uid = args["uid"]
    refused = _not_signed_in(call)
    if refused is not None:
        return refused
    if call.icloud_apple_id is None:
        outcome = await mail_ops.archive_message(provider, uid, mailbox)
    else:
        outcome = await mail_ops.archive_message(
            provider,
            uid,
            mailbox,
            apple_id=call.icloud_apple_id,
            imap_user=call.icloud_imap_user or call.icloud_apple_id,
        )
    return f"Archived message {uid} from {mailbox} ({provider}): {outcome}"


def _icloud(brain_name: str) -> ToolFunc:
    async def body(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await _bridge(env, call, brain_name, dict(args))

    return body


def _direct(body: Callable[[dict[str, Any]], Awaitable[str]]) -> ToolFunc:
    async def run(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await body(args)

    return run


def _for_call(body: Callable[[dict[str, Any], Call], Awaitable[str]]) -> ToolFunc:
    async def run(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await body(args, call)

    return run


def _zoned(body: Callable[[dict[str, Any], ZoneInfo, Call], Awaitable[str]]) -> ToolFunc:
    async def run(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await body(args, env.zone, call)

    return run


class _AppleIntegration:
    name = ""

    def enabled(self, config: ConnectorConfig) -> bool:
        return bool(getattr(config.integrations, self.name))


class AppleReminders(_AppleIntegration):
    name = "reminders"

    def tools(self, config: ConnectorConfig) -> list[ToolDef]:
        return [
            ToolDef(
                name="reminders_list",
                description="List open Apple Reminders in every list or in one list; it never changes a reminder.",
                schema=brain_schema("reminders_list"),
                func=_reminders_list,
                full=True,
                limited=True,
                read_only=True,
                destructive=False,
            ),
            ToolDef(
                name="reminder_create",
                description=(
                    "Create an Apple Reminder with a due time and an alarm in the named list; it never "
                    "changes or removes another reminder."
                ),
                schema=object_schema(
                    {
                        "title": string_schema("What to do."),
                        "due": string_schema(_DUE),
                        "alarm": string_schema(
                            "When the alarm rings, written like due. Default: at the due time."
                        ),
                        "notes": string_schema("Optional notes."),
                        "list_name": string_schema(
                            "The exact Reminders list, from the routing table."
                        ),
                    },
                    ["title", "due", "list_name"],
                ),
                func=_for_call(reminder_create),
                full=True,
                limited=True,
                read_only=False,
                destructive=False,
            ),
            ToolDef(
                name="reminder_update",
                description=(
                    "Change the title, notes, due time or alarm of one reminder; it never deletes a "
                    "reminder."
                ),
                schema=object_schema(
                    {
                        "id": string_schema("The reminder's identifier from reminders_list."),
                        "title": string_schema("New title."),
                        "notes": string_schema("New notes."),
                        "due": string_schema(_DUE),
                        "alarm": string_schema("When the alarm rings, written like due."),
                    },
                    ["id"],
                ),
                func=_for_call(reminder_update),
                full=True,
                limited=False,
                read_only=False,
                destructive=True,
            ),
            ToolDef(
                name="reminder_complete",
                description="Mark one reminder as done; it never deletes a reminder.",
                schema=object_schema(
                    {"id": string_schema("The reminder's identifier from reminders_list.")},
                    ["id"],
                ),
                func=_for_call(reminder_complete),
                full=True,
                limited=False,
                read_only=False,
                destructive=True,
            ),
        ]


class AppleCalendar(_AppleIntegration):
    name = "calendar"

    def tools(self, config: ConnectorConfig) -> list[ToolDef]:
        return [
            ToolDef(
                name="calendar_list",
                description="List iCloud calendar events in a date range; it never changes an event.",
                schema=brain_schema("calendar_list_events"),
                func=_calendar_list,
                full=True,
                limited=True,
                read_only=True,
                destructive=False,
            ),
            ToolDef(
                name="calendar_create",
                description=(
                    "Create an iCloud calendar event with an alert, 60 minutes before by default; it "
                    "never invites anyone or changes another event."
                ),
                schema=object_schema(
                    {
                        "summary": string_schema("Title of the event."),
                        "start": string_schema(
                            "Start, 'YYYY-MM-DDTHH:MM'; a time without an offset is local time. "
                            "A date if all_day."
                        ),
                        "end": string_schema("End, written like start."),
                        "all_day": {"type": "boolean"},
                        "location": string_schema("Where."),
                        "description": string_schema("Optional details."),
                        "calendar_name": string_schema("Calendar to use, from the routing table."),
                        "alert_minutes": _ALERT,
                    },
                    ["summary", "start", "end"],
                ),
                func=_zoned(calendar_create),
                full=True,
                limited=False,
                read_only=False,
                destructive=True,
            ),
            ToolDef(
                name="calendar_update",
                description=(
                    "Change the time, title, place, details or alert of one calendar event; it never "
                    "deletes or cancels an event."
                ),
                schema=object_schema(
                    {
                        "event_url": string_schema("The event URL from calendar_list."),
                        "summary": string_schema("New title."),
                        "start": string_schema(
                            "New start, 'YYYY-MM-DDTHH:MM'; a time without an offset is local time."
                        ),
                        "end": string_schema("New end, written like start."),
                        "location": string_schema("New place."),
                        "description": string_schema("New details."),
                        "alert_minutes": _ALERT,
                    },
                    ["event_url"],
                ),
                func=_zoned(calendar_update),
                full=True,
                limited=False,
                read_only=False,
                destructive=True,
            ),
        ]


class AppleMail(_AppleIntegration):
    name = "mail"

    def tools(self, config: ConnectorConfig) -> list[ToolDef]:
        return [
            ToolDef(
                name="mail_search",
                description="Search iCloud mail and return short summaries of the messages; it never sends, changes or deletes mail.",
                schema=brain_schema("mail_search"),
                func=_icloud("mail_search"),
                full=True,
                limited=True,
                read_only=True,
                destructive=False,
            ),
            ToolDef(
                name="mail_read",
                description="Read one iCloud mail message by its UID; it never sends, changes or deletes mail.",
                schema=brain_schema("mail_get_message"),
                func=_icloud("mail_get_message"),
                full=True,
                limited=True,
                read_only=True,
                destructive=False,
            ),
            ToolDef(
                name="mail_archive",
                description="Move one mail message to the Archive mailbox; it never deletes or sends mail.",
                schema=object_schema(
                    {
                        "uid": string_schema("Message UID from mail_search."),
                        "mailbox": string_schema("Mailbox it is in. Default INBOX."),
                        "provider": dict(ICLOUD_ONLY),
                    },
                    ["uid"],
                ),
                func=_for_call(mail_archive),
                full=True,
                limited=False,
                read_only=False,
                destructive=True,
            ),
        ]


class AppleContacts(_AppleIntegration):
    name = "contacts"

    def tools(self, config: ConnectorConfig) -> list[ToolDef]:
        return [
            ToolDef(
                name="contacts_search",
                description="Look up contacts by name, email, phone or organisation; it never changes a contact.",
                schema=brain_schema("contacts_search"),
                func=_icloud("contacts_search"),
                full=True,
                limited=True,
                read_only=True,
                destructive=False,
            ),
        ]
