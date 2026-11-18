"""The vault connector's vault tools: one table, and the bodies behind it.

``run_tool`` checks the arguments against the schema, runs the body and turns
every exception into text that starts with ``ERROR:``, without the vault
folder's place on disk, so no exception leaves a tool.

The vault functions run in worker threads. Every vault write (append, create,
open item, edit, confirm, undo) holds ``ToolEnv.write_lock``, so one vault
write runs at a time. A body runs to its end once started, even when the call
is cancelled, so a cancel never releases the lock while a write is running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

import anyio

from src.brain.connector.config import system_timezone
from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout, owner_only_paths
from src.brain.connector.people import DEFAULT_PERSON_KEY, DEFAULT_PERSON_NAME
from src.brain.connector.vault import actions, edit, read, write
from src.brain.connector.vault.coordination import StaleVersion
from src.brain.connector.vault.edit import EditRefused, EditResult
from src.brain.connector.vault.paths import PathRefused
from src.brain.connector.vault.read import NotDownloaded, ReadResult
from src.brain.connector.vault.undo_store import UndoStore
from src.brain.connector.vault.write import NameExists
from src.gateway.validation import SchemaValidationError, validate_arguments

_logger = logging.getLogger(__name__)

class ICloudCall(Protocol):
    """Run one iCloud brain tool by name with its arguments and return its text.

    The third argument, given only when it is not empty, is extra environment
    for the tool's process: the iCloud account of the person who calls.
    """

    def __call__(
        self, name: str, arguments: dict[str, Any], extra_env: Mapping[str, str] | None = ..., /
    ) -> str: ...


@dataclass
class ToolEnv:
    """What every tool body shares."""

    vault: Path
    undo: UndoStore
    icloud: ICloudCall | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    zone: ZoneInfo = field(default_factory=lambda: ZoneInfo(system_timezone()))
    layout: VaultLayout = DEFAULT_LAYOUT
    person_names: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Call:
    """What one call knows about who makes it: the person, their layout, lists and calendars."""

    mail_read_recently: bool = False
    person: str = DEFAULT_PERSON_KEY
    person_name: str = DEFAULT_PERSON_NAME
    layout: VaultLayout | None = None  # None: the connector's own layout, ``ToolEnv.layout``
    reminder_lists: tuple[str, ...] | None = None
    calendars: tuple[str, ...] | None = None
    icloud_apple_id: str | None = None
    icloud_imap_user: str | None = None
    default_icloud: bool = True  # the Mac's own iCloud account is used only at the empty prefix


ToolFunc = Callable[[ToolEnv, Call, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict[str, Any]
    func: ToolFunc
    full: bool
    limited: bool
    read_only: bool
    destructive: bool


# Text helpers


def visible(text: str) -> str:
    """``text`` with invisible and control characters written out, ``repr`` style.

    A line break is shown as ``\\n`` and then kept, so the passage still reads
    line by line.
    """
    out: list[str] = []
    for char in text:
        if char == "\n":
            out.append("\\n\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif char == "\\":
            out.append("\\\\")
        elif char != " " and unicodedata.category(char)[0] in "CZ":
            code = ord(char)
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(char)
    return "".join(out)


def _note(result: ReadResult, first: str) -> str:
    return f"{first}\npath: {result.path}\nhash: {result.hash}\nlines: {result.total_lines}\n\n{result.text}"


HELD_MARK = "Edit held for "
HELD_RULE = "Rule that held it: "


def held_prefix(name: str) -> str:
    """The first line of a held edit's text, naming the person who must approve it."""
    return f"{HELD_MARK}{name}'s approval; nothing was written."


def _held(rel: str, old: str, new: str, result: EditResult, name: str) -> str:
    return (
        f"{held_prefix(name)}\n"
        f"{HELD_RULE}{result.reason}\n"
        f"Note: {rel}\n"
        f"Old passage:\n{visible(old)}\n"
        f"New passage:\n{visible(new)}\n"
        f"held_id: {result.held_id}\n"
        "Call vault_edit_confirm with this id to run it."
    )


def _done(result: EditResult) -> str:
    assert result.read_back is not None
    return _note(
        result.read_back,
        f"Done. edit_id: {result.edit_id} (vault_undo with this id puts the old passage back)\n"
        f"Rule check: {result.reason}",
    )


def _text_arg(args: Mapping[str, Any], name: str, default: str = "") -> str:
    value = args.get(name)
    return value if isinstance(value, str) else default


def _date_arg(args: Mapping[str, Any], name: str) -> date | None:
    value = _text_arg(args, name).strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date written YYYY-MM-DD") from None


# Vault bodies


async def _vault_search(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    field_name, field_value = _text_arg(args, "field_name"), _text_arg(args, "field_value")
    if bool(field_name) != bool(field_value):
        raise ValueError("give field_name and field_value together")
    matches = await asyncio.to_thread(
        read.search,
        env.vault,
        _text_arg(args, "query"),
        _text_arg(args, "folder") or None,
        (field_name, field_value) if field_name else None,
        int(args.get("limit", 50)),
    )
    if not matches:
        return "No matches."
    return "\n".join(f"{m.path}:{m.line}: {m.text}" for m in matches)


async def _vault_read(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    start, end = args.get("start"), args.get("end")
    result = await asyncio.to_thread(read.read_note, env.vault, args["path"], start, end)
    first = "Note read."
    if start is not None or end is not None:
        first = f"Lines {start or 1} to {end or result.total_lines} of the note."
    return _note(result, first)


async def _vault_list(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    names = await asyncio.to_thread(read.list_folder, env.vault, _text_arg(args, "path"))
    return "\n".join(names) if names else "The folder is empty."


async def _vault_append(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    """Append to a note, by default the caller's daily note, made from the caller's template.

    The owner-only paths come from the connector's layout, never the caller's.
    """
    now = datetime.now(env.zone)
    layout = call.layout or env.layout
    rel = _text_arg(args, "path") or write.daily_note_path(now, layout)
    heading = _text_arg(args, "heading") or None
    async with env.write_lock:
        result = await asyncio.to_thread(
            partial(
                write.append,
                env.vault,
                rel,
                args["text"],
                heading,
                now,
                layout=layout,
                owner_only=owner_only_paths(env.layout),
            )
        )
    return _note(result, "Added. The note now reads:")


async def _vault_create(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    async with env.write_lock:
        result = await asyncio.to_thread(
            write.create, env.vault, args["path"], args["text"], layout=env.layout
        )
    return _note(result, "Created. The note reads:")


async def _vault_edit(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    rel, old, new = args["path"], args["old"], args["new"]
    async with env.write_lock:
        result = await asyncio.to_thread(
            edit.apply_edit,
            env.vault,
            env.undo,
            rel,
            old,
            new,
            args["expected_hash"],
            args["reason"],
            call.mail_read_recently,
            actions_dir=env.layout.actions_dir,
            owner_only=owner_only_paths(env.layout),
            person=call.person,
        )
    if result.status == "held":
        return _held(rel, old, new, result, call.person_name)
    return _done(result)


async def _vault_edit_confirm(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    async with env.write_lock:
        result = await asyncio.to_thread(
            edit.confirm_held,
            env.vault,
            env.undo,
            args["held_id"],
            call.person,
            {**env.person_names, call.person: call.person_name},
            owner_only=owner_only_paths(env.layout),
        )
    return _done(result)


async def _vault_undo(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    async with env.write_lock:
        result = await asyncio.to_thread(
            edit.undo,
            env.vault,
            env.undo,
            args["edit_id"],
            owner_only=owner_only_paths(env.layout),
        )
    return _note(result, "Undone. The note now reads:")


async def _open_item_add(env: ToolEnv, call: Call, args: dict[str, Any]) -> str:
    """Add an open item; it belongs to the caller unless ``owner`` names someone else.

    A connector without people writes no owner, as before people existed.
    """
    status = cast(Literal["next", "waiting"], args["status"])
    due, follow_up = _date_arg(args, "due"), _date_arg(args, "follow_up")
    named = call.person != DEFAULT_PERSON_KEY
    owner = (_text_arg(args, "owner") or call.person) if named else None
    async with env.write_lock:
        result = await asyncio.to_thread(
            partial(
                actions.open_item_add,
                env.vault,
                args["outcome"],
                status,
                due,
                _text_arg(args, "waiting_on") or None,
                follow_up,
                _text_arg(args, "source"),
                datetime.now(env.zone).date(),
                owner=owner,
                assigned_by=call.person if named else None,
                layout=env.layout,
            )
        )
    return _note(result, "Open item created:")


# Schemas


def string_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


_PATH = string_schema("Path of the note inside the vault, for example 'Notes/Appointments.md'.")
_DATE = "A date written YYYY-MM-DD."

_VAULT_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name="vault_search",
        description=(
            "Search the vault's notes for words, optionally inside one folder or only in notes "
            "whose frontmatter field has a given value, and return the matching lines; it never "
            "changes anything."
        ),
        schema=object_schema(
            {
                "query": string_schema("Words to find, matched without regard to case."),
                "folder": string_schema("Only search inside this vault folder."),
                "field_name": string_schema("Only notes whose frontmatter has this field..."),
                "field_value": string_schema("...with this value."),
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
        func=_vault_search,
        full=True,
        limited=True,
        read_only=True,
        destructive=False,
    ),
    ToolDef(
        name="vault_read",
        description=(
            "Read a note, or a range of its lines, and return its text with the content hash "
            "that vault_edit needs; it never changes anything."
        ),
        schema=object_schema(
            {
                "path": _PATH,
                "start": {"type": "integer", "minimum": 1, "description": "First line, from 1."},
                "end": {"type": "integer", "minimum": 1, "description": "Last line, included."},
            },
            ["path"],
        ),
        func=_vault_read,
        full=True,
        limited=True,
        read_only=True,
        destructive=False,
    ),
    ToolDef(
        name="vault_list",
        description="List the notes and folders in a vault folder; it never changes anything.",
        schema=object_schema(
            {"path": string_schema("Folder inside the vault; empty for the top.")}
        ),
        func=_vault_list,
        full=True,
        limited=True,
        read_only=True,
        destructive=False,
    ),
    ToolDef(
        name="vault_append",
        description=(
            "Add text to the end of a note or of one heading's section, creating today's daily "
            "note from the template when it is missing; it never overwrites or removes text."
        ),
        schema=object_schema(
            {
                "path": string_schema("Path of the note; leave it out for today's daily note."),
                "text": string_schema("The text to add."),
                "heading": string_schema(
                    "Add under this heading; a new one is made if none matches."
                ),
            },
            ["text"],
        ),
        func=_vault_append,
        full=True,
        limited=True,
        read_only=False,
        destructive=False,
    ),
    ToolDef(
        name="vault_create",
        description=(
            "Create a new note, its name ending in .md; it never replaces a note and refuses a "
            "name that any note in the vault already has."
        ),
        schema=object_schema(
            {"path": _PATH, "text": string_schema("The whole note.")}, ["path", "text"]
        ),
        func=_vault_create,
        full=True,
        limited=True,
        read_only=False,
        destructive=False,
    ),
    ToolDef(
        name="vault_edit",
        description=(
            "Replace one exact passage in a note you have just read, at once when the change is "
            "small and otherwise held until the person you are working with approves it; it never deletes or moves a note."
        ),
        schema=object_schema(
            {
                "path": _PATH,
                "old": string_schema(
                    "The exact passage to replace; it must appear once in the note."
                ),
                "new": string_schema("The text to put in its place."),
                "expected_hash": string_schema("The hash vault_read returned for this note."),
                "reason": string_schema("One sentence saying why."),
            },
            ["path", "old", "new", "expected_hash", "reason"],
        ),
        func=_vault_edit,
        full=True,
        limited=False,
        read_only=False,
        destructive=False,
    ),
    ToolDef(
        name="vault_edit_confirm",
        description=(
            "Run an edit that vault_edit held, once the owner has approved it; it never runs when "
            "the note changed after the edit was held."
        ),
        schema=object_schema(
            {"held_id": string_schema("The held_id vault_edit returned.")}, ["held_id"]
        ),
        func=_vault_edit_confirm,
        full=True,
        limited=False,
        read_only=False,
        destructive=True,
    ),
    ToolDef(
        name="vault_undo",
        description=(
            "Put back the passage an earlier vault_edit replaced; it never overwrites a note that "
            "changed after that edit."
        ),
        schema=object_schema(
            {"edit_id": string_schema("The edit_id the edit returned.")}, ["edit_id"]
        ),
        func=_vault_undo,
        full=True,
        limited=False,
        read_only=False,
        destructive=True,
    ),
    ToolDef(
        name="open_item_add",
        description=(
            "Record one thing that is left open as a new note in the open-items folder, with status next or "
            "waiting; it never changes an existing open item."
        ),
        schema=object_schema(
            {
                "outcome": string_schema("What done looks like, in a few words."),
                "status": {"type": "string", "enum": ["next", "waiting"]},
                "due": string_schema(f"Optional due date. {_DATE}"),
                "waiting_on": string_schema("Who it waits on; needed for status waiting."),
                "follow_up": string_schema(
                    f"When to follow up; needed for status waiting. {_DATE}"
                ),
                "source": string_schema("Where it came from, in a few words."),
            },
            ["outcome", "status"],
        ),
        func=_open_item_add,
        full=True,
        limited=True,
        read_only=False,
        destructive=False,
    ),
)


OWNER_BOTH = "both"


def _with_owner(tool: ToolDef, people: tuple[str, ...]) -> ToolDef:
    """``open_item_add`` with an ``owner`` argument naming each person, or both."""
    schema = deepcopy(tool.schema)
    schema["properties"]["owner"] = {
        "type": "string",
        "enum": [*people, OWNER_BOTH],
        "description": "Whose task this is. Default: the person you are working with.",
    }
    return replace(tool, schema=schema)


def vault_tools(people: tuple[str, ...] = ()) -> list[ToolDef]:
    """A new list of the vault tools.

    ``people`` are the keys of the people in the settings; with more than one,
    ``open_item_add`` takes an ``owner``.
    """
    if len(people) < 2:
        return list(_VAULT_TOOLS)
    return [
        _with_owner(tool, people) if tool.name == "open_item_add" else tool
        for tool in _VAULT_TOOLS
    ]


_TARGET_KEYS = (
    "path",
    "held_id",
    "edit_id",
    "id",
    "uid",
    "event_url",
    "outcome",
    "title",
    "summary",
    "query",
    "folder",
    "list_name",
    "date_from",
)
_LONG_TEXT_KEYS = frozenset({"old", "new", "text", "notes", "description"})


def audit_target(args: Mapping[str, Any]) -> str:
    """What a call acts on, for the audit log: the first of the usual argument names it has."""
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def audit_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments for the audit log, with note and passage text replaced by its length.

    The text an edit replaces belongs in the undo store, not in the audit log.
    """
    return {
        key: f"<{len(value)} characters>"
        if key in _LONG_TEXT_KEYS and isinstance(value, str)
        else value
        for key, value in args.items()
    }


def _plain(message: str, env: ToolEnv) -> str:
    """``message`` without the vault folder's place on disk."""
    for root in sorted(
        {str(env.vault.expanduser().resolve()), str(env.vault)}, key=len, reverse=True
    ):
        message = message.replace(root + os.sep, "").replace(root, "the vault")
    return message


async def run_tool(tool: ToolDef, env: ToolEnv, args: dict[str, Any], call: Call) -> str:
    """Run one call of ``tool`` and return its text; every failure becomes ``ERROR: ...`` text."""
    try:
        validate_arguments(args, tool.schema)
        with anyio.CancelScope(shield=True):
            text = await tool.func(env, call, args)
        return text
    except SchemaValidationError as error:
        return f"ERROR: invalid arguments for {tool.name}: {error}"
    except FileNotFoundError as error:
        missing = error.filename or (error.args[0] if error.args else "")
        return _plain(f"ERROR: nothing at {missing}", env)
    except (PathRefused, StaleVersion, NotDownloaded, NameExists, EditRefused, ValueError) as error:
        return _plain(f"ERROR: {error}", env)
    except Exception as error:
        _logger.warning("tool %s failed: %s", tool.name, type(error).__name__)
        return _plain(f"ERROR: {type(error).__name__}: {error}", env)
