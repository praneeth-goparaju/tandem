"""The connector's server instructions: a tracked template plus the owner's overlay.

The overlay is the private ``user/connector_instructions.yaml``; the example is
``examples/connector_instructions.example.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from src.brain.connector.config import REPO_ROOT, Integrations
from src.brain.connector.layout import VaultLayout

TEMPLATE_PATH = REPO_ROOT / "config" / "connector_instructions.md.j2"
MAX_LINES = 60
EM_DASH = chr(0x2014)
_TOP_KEYS = frozenset(
    {"owner_name", "privacy_rule", "routing_rows", "source_of_truth", "extra_rules"}
)
_ROUTING_KEYS = ("who", "list", "calendar")
_SOURCE_KEYS = ("topic", "file")


@dataclass(frozen=True)
class RoutingRow:
    who: str
    list: str
    calendar: str = "none"


@dataclass(frozen=True)
class SourceRow:
    topic: str
    file: str


@dataclass(frozen=True)
class InstructionsOverlay:
    owner_name: str = "the owner"
    privacy_rule: str = ""
    routing_rows: tuple[RoutingRow, ...] = ()
    source_of_truth: tuple[SourceRow, ...] = ()
    extra_rules: tuple[str, ...] = ()


class InstructionsTooLong(ValueError):
    """The rendered instructions have more lines than claude.ai should be sent."""

    def __init__(self, lines: int) -> None:
        super().__init__(
            f"the server instructions are {lines} lines long; the limit is {MAX_LINES}. "
            "Shorten source_of_truth, routing_rows or extra_rules in "
            "connector_instructions.yaml"
        )
        self.lines = lines


def _text(value: object, key: str, source: Path, *, cell: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {key} must be a non-empty string")
    text = value.strip()
    if len(text.splitlines()) != 1:
        raise ValueError(f"{source}: {key} must be one line")
    if EM_DASH in text:
        raise ValueError(f"{source}: em dash in {key}")
    if cell and "|" in text:
        raise ValueError(f"{source}: {key} must not contain |, which would break the table")
    return text


def _rows(
    raw: dict[str, Any],
    key: str,
    fields: tuple[str, ...],
    required: int,
    source: Path,
    *,
    label: str | None = None,
) -> list[dict[str, str]]:
    label = label or key
    items = raw.get(key) or []
    if not isinstance(items, list):
        raise ValueError(f"{source}: {label} must be a list")
    rows: list[dict[str, str]] = []
    for index, item in enumerate(items):
        where = f"{label}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{source}: {where} must be a mapping")
        unknown = sorted(str(name) for name in item if name not in fields)
        if unknown:
            raise ValueError(f"{source}: {where}.{unknown[0]} is not a known key")
        for name in fields[:required]:
            if name not in item:
                raise ValueError(f"{source}: {where}.{name} is required")
        rows.append(
            {
                name: _text(item[name], f"{where}.{name}", source, cell=True)
                for name in fields
                if name in item
            }
        )
    return rows


def _profile_routing(profile_path: Path | None) -> tuple[RoutingRow, ...]:
    if profile_path is None or not profile_path.is_file():
        return ()
    with profile_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict) or raw.get("reminders") is None:
        return ()
    reminders = raw["reminders"]
    if not isinstance(reminders, dict):
        raise ValueError(f"{profile_path}: reminders must be a mapping")
    rows = _rows(
        reminders, "routing", _ROUTING_KEYS, 2, profile_path, label="reminders.routing"
    )
    return tuple(RoutingRow(**row) for row in rows)


def load_overlay(path: Path | None, profile_path: Path | None = None) -> InstructionsOverlay:
    """Read the owner's overlay from ``path``; a missing file gives the defaults."""
    overlay = _load_overlay_file(path)
    if overlay.routing_rows:
        return overlay
    profile_rows = _profile_routing(profile_path)
    if not profile_rows:
        return overlay
    return replace(overlay, routing_rows=profile_rows)


def _load_overlay_file(path: Path | None) -> InstructionsOverlay:
    if path is None or not path.is_file():
        return InstructionsOverlay()
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        return InstructionsOverlay()
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    unknown = sorted(str(key) for key in raw if key not in _TOP_KEYS)
    if unknown:
        raise ValueError(f"{path}: {unknown[0]} is not a known key")
    owner_name = raw.get("owner_name")
    privacy_rule = raw.get("privacy_rule") or ""
    extra = raw.get("extra_rules") or []
    if not isinstance(extra, list):
        raise ValueError(f"{path}: extra_rules must be a list")
    return InstructionsOverlay(
        owner_name="the owner" if owner_name is None else _text(owner_name, "owner_name", path),
        privacy_rule=_text(privacy_rule, "privacy_rule", path) if privacy_rule else "",
        routing_rows=tuple(
            RoutingRow(**row) for row in _rows(raw, "routing_rows", _ROUTING_KEYS, 2, path)
        ),
        source_of_truth=tuple(
            SourceRow(**row) for row in _rows(raw, "source_of_truth", _SOURCE_KEYS, 2, path)
        ),
        extra_rules=tuple(
            _text(rule, f"extra_rules[{index}]", path) for index, rule in enumerate(extra)
        ),
    )


def render_instructions(
    overlay: InstructionsOverlay, layout: VaultLayout, integrations: Integrations
) -> str:
    """Render the server instructions for this overlay, layout and set of integrations."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=select_autoescape([]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    text = env.get_template(TEMPLATE_PATH.name).render(
        owner=overlay.owner_name,
        privacy_rule=overlay.privacy_rule,
        extra_rules=overlay.extra_rules,
        routing_rows=overlay.routing_rows,
        source_of_truth=overlay.source_of_truth,
        reminders_or_calendar=integrations.reminders or integrations.calendar,
        actions_dir=layout.actions_dir,
        daily_note_pattern=f"{layout.daily_notes_dir}/YYYY/MM/YYYY-MM-DD.md",
    )
    if EM_DASH in text:
        raise ValueError("em dash in the rendered server instructions")
    lines = len(text.splitlines())
    if lines > MAX_LINES:
        raise InstructionsTooLong(lines)
    return text
