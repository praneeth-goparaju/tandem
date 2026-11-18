"""The people who sign in to the vault connector, read from the ``people`` section of the settings.

Each person has their own pair of addresses, ``/<prefix>/mcp`` and
``/<prefix>/mcp/limited``, their own password and one-time code, their own
tool list and instructions, and their own daily notes. Exactly one person has
the empty prefix and so the addresses ``/mcp`` and ``/mcp/limited``; sign-in
state from before people existed belongs to that person.

Without a ``people`` section there is one person, with the key ``owner``, the
empty prefix, every tool, every list and every calendar, which is how the
connector behaved before.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from src.brain.connector.layout import VaultLayout, _checked

DEFAULT_PERSON_KEY = "owner"
DEFAULT_PERSON_NAME = "the owner"
INSTRUCTIONS_OVERLAY_NAME = "connector_instructions.yaml"
RESERVED_PREFIXES = frozenset(
    {"mcp", "login", "health", "authorize", "token", "register", "revoke", ".well-known"}
)
_KEY = re.compile(r"[a-z0-9-]+")
_PERSON_KEYS = frozenset(
    {
        "name",
        "prefix",
        "instructions",
        "daily_notes_dir",
        "daily_note_template",
        "icloud_apple_id",
        "icloud_imap_user",
        "exclude_tools",
        "reminder_lists",
        "calendars",
    }
)

Access = Literal["full", "limited"]


@dataclass(frozen=True)
class Person:
    """One person who signs in: their addresses, tools, accounts, lists and daily notes."""

    key: str
    name: str
    prefix: str
    excluded_tools: frozenset[str]
    icloud_apple_id: str | None
    icloud_imap_user: str | None
    reminder_lists: tuple[str, ...] | None
    calendars: tuple[str, ...] | None
    layout: VaultLayout
    instructions_overlay: Path

    def path(self, access: Access) -> str:
        """The address path of this person's full or limited endpoint."""
        base = f"/{self.prefix}/mcp" if self.prefix else "/mcp"
        return base if access == "full" else f"{base}/limited"


def default_person(layout: VaultLayout, instructions_overlay: Path) -> Person:
    """The one person of a connector whose settings have no ``people`` section."""
    return Person(
        key=DEFAULT_PERSON_KEY,
        name=DEFAULT_PERSON_NAME,
        prefix="",
        excluded_tools=frozenset(),
        icloud_apple_id=None,
        icloud_imap_user=None,
        reminder_lists=None,
        calendars=None,
        layout=layout,
        instructions_overlay=instructions_overlay,
    )


def _text(value: object, where: str, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {where} must be a non-empty string")
    return value.strip()


def _optional_text(entry: Mapping[str, Any], name: str, where: str, source: Path) -> str | None:
    if entry.get(name) is None:
        return None
    return _text(entry[name], f"{where}.{name}", source)


def _names(entry: Mapping[str, Any], name: str, where: str, source: Path) -> tuple[str, ...] | None:
    value = entry.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{source}: {where}.{name} must be a list of names")
    return tuple(_text(item, f"{where}.{name}", source) for item in value)


def _prefix(entry: Mapping[str, Any], where: str, source: Path) -> str:
    value = entry.get("prefix")
    if not isinstance(value, str):
        raise ValueError(f"{source}: {where}.prefix is required; write \"\" for /mcp")
    prefix = value.strip()
    if prefix and not _KEY.fullmatch(prefix):
        raise ValueError(
            f"{source}: {where}.prefix {prefix!r} may only hold lower case letters, digits and -"
        )
    if prefix in RESERVED_PREFIXES:
        raise ValueError(f"{source}: {where}.prefix {prefix!r} is an address the connector uses")
    return prefix


def _overlay(
    entry: Mapping[str, Any], key: str, prefix: str, where: str, source: Path, user_dir: Path
) -> Path:
    default = INSTRUCTIONS_OVERLAY_NAME if not prefix else f"connector_instructions.{key}.yaml"
    name = _optional_text(entry, "instructions", where, source) or default
    path = Path(name).expanduser()
    return path if path.is_absolute() else user_dir / path


def _layout(entry: Mapping[str, Any], base: VaultLayout, where: str, source: Path) -> VaultLayout:
    changes: dict[str, str] = {}
    for name in ("daily_notes_dir", "daily_note_template"):
        if entry.get(name) is None:
            continue
        try:
            changes[name] = _checked(name, entry[name], source)
        except ValueError as error:
            raise ValueError(str(error).replace(f"notes.{name}", f"{where}.{name}")) from None
    return replace(base, **changes)


def _excluded(
    entry: Mapping[str, Any], where: str, source: Path, tool_names: Collection[str] | None
) -> frozenset[str]:
    names = _names(entry, "exclude_tools", where, source) or ()
    if tool_names is not None:
        unknown = sorted(name for name in names if name not in tool_names)
        if unknown:
            raise ValueError(
                f"{source}: {where}.exclude_tools names {unknown[0]}, which is not a tool"
            )
    return frozenset(names)


def _person(
    key: str,
    entry: object,
    source: Path,
    base_layout: VaultLayout,
    user_dir: Path,
    tool_names: Collection[str] | None,
) -> Person:
    where = f"people.{key}"
    if not _KEY.fullmatch(key):
        raise ValueError(f"{source}: {where}: a key may only hold lower case letters, digits and -")
    if key == DEFAULT_PERSON_KEY:
        raise ValueError(
            f"{source}: {where}: the key {DEFAULT_PERSON_KEY} is kept for a connector without people"
        )
    if not isinstance(entry, dict):
        raise ValueError(f"{source}: {where} must be a mapping")
    unknown = sorted(str(name) for name in entry if name not in _PERSON_KEYS)
    if unknown:
        raise ValueError(f"{source}: {where}.{unknown[0]} is not a known key")
    prefix = _prefix(entry, where, source)
    return Person(
        key=key,
        name=_text(entry.get("name"), f"{where}.name", source),
        prefix=prefix,
        excluded_tools=_excluded(entry, where, source, tool_names),
        icloud_apple_id=_optional_text(entry, "icloud_apple_id", where, source),
        icloud_imap_user=_optional_text(entry, "icloud_imap_user", where, source),
        reminder_lists=_names(entry, "reminder_lists", where, source),
        calendars=_names(entry, "calendars", where, source),
        layout=_layout(entry, base_layout, where, source),
        instructions_overlay=_overlay(entry, key, prefix, where, source, user_dir),
    )


def load_people(
    raw: dict[str, Any],
    source: Path,
    base_layout: VaultLayout,
    user_dir: Path,
    *,
    tool_names: Collection[str] | None = None,
) -> tuple[Person, ...]:
    """Read the ``people`` section of the settings in ``raw``.

    Without one there is one person, the default. Raises ``ValueError``,
    naming the person's key, for a bad key or value, two people with the same
    prefix, no person with the empty prefix, a prefix the connector uses for
    its own addresses, or an excluded tool that is not in ``tool_names``.
    """
    section = raw.get("people")
    if section is None:
        return (default_person(base_layout, user_dir / INSTRUCTIONS_OVERLAY_NAME),)
    if not isinstance(section, dict) or not section:
        raise ValueError(f"{source}: people must be a mapping with at least one person")
    people = tuple(
        _person(str(key), entry, source, base_layout, user_dir, tool_names)
        for key, entry in section.items()
    )
    seen: dict[str, str] = {}
    for person in people:
        if person.prefix in seen:
            raise ValueError(
                f"{source}: people.{person.key} has the prefix {person.prefix!r} "
                f"of people.{seen[person.prefix]}"
            )
        seen[person.prefix] = person.key
    if "" not in seen:
        keys = ", ".join(person.key for person in people)
        raise ValueError(f"{source}: one person ({keys}) must have the prefix \"\" for /mcp")
    return people


def root_person(people: tuple[Person, ...]) -> Person:
    """The person with the empty prefix, who owns the sign-in state from before people existed."""
    return next(person for person in people if not person.prefix)
