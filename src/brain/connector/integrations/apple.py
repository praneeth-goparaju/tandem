"""The Apple integrations of the vault connector: Reminders, Calendar, Mail and Contacts.

The writes call the action handlers and ``mail_ops.archive_message`` directly:
the owner's Approve tap in claude.ai is the approval, so nothing goes through
the daemon's action queue.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import date, datetime
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


def icloud_bridge(name: str, arguments: dict[str, Any]) -> str:
    """Run the brain tool ``name`` in its own process, as the gateway does, under the read profile.

    Blocks until the process ends, so call it from a worker thread.
    """
    backend = CommandBackend(BridgeSettings.from_environment(os.environ))
    profile = load_profile({"BRAIN_MCP_PROFILE": "read"})
    result = backend.invoke(
        "call",
        {
            "name": name,
            "arguments": arguments,
            "allowed": [name],
            "caller_context": dict(profile.caller_context),
        },
    )
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


async def reminder_create(args: dict[str, Any]) -> str:
    return _handler_text(await create_reminder.execute(dict(args)))


async def reminder_update(args: dict[str, Any]) -> str:
    return _handler_text(await update_reminder.execute(dict(args)))


async def reminder_complete(args: dict[str, Any]) -> str:
    return _handler_text(await complete_reminder.execute(dict(args)))


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


async def calendar_create(args: dict[str, Any], zone: ZoneInfo) -> str:
    payload = {"alert_minutes": DEFAULT_ALERT_MINUTES, **_local_times(args, zone)}
    return _handler_text(await create_calendar_event.execute(payload))


async def calendar_update(args: dict[str, Any], zone: ZoneInfo) -> str:
    """Update an event, unless it has attendees or an organizer.

    iCloud tells the other people about a change to such an event, and the
    connector never sends anything.
    """
    if await update_calendar_event.event_involves_others(args["event_url"]):
        return OTHERS_REFUSED
    return _handler_text(await update_calendar_event.execute(_local_times(args, zone)))


async def mail_archive(args: dict[str, Any]) -> str:
    provider = args.get("provider") or "icloud"
    mailbox = args.get("mailbox") or "INBOX"
    uid = args["uid"]
    outcome = await mail_ops.archive_message(provider, uid, mailbox)
    return f"Archived message {uid} from {mailbox} ({provider}): {outcome}"


def _icloud(brain_name: str) -> ToolFunc:
    async def body(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        bridge: ICloudCall = env.icloud or icloud_bridge
        return await asyncio.to_thread(bridge, brain_name, dict(args))

    return body


def _direct(body: Callable[[dict[str, Any]], Awaitable[str]]) -> ToolFunc:
    async def run(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await body(args)

    return run


def _zoned(body: Callable[[dict[str, Any], ZoneInfo], Awaitable[str]]) -> ToolFunc:
    async def run(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
        return await body(args, env.zone)

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
                func=_icloud("reminders_list"),
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
                func=_direct(reminder_create),
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
                func=_direct(reminder_update),
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
                func=_direct(reminder_complete),
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
                func=_icloud("calendar_list_events"),
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
                func=_direct(mail_archive),
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
