from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any

from src.orchestrator.runners.run_guard import BRAIN_TOOL_PREFIX

log = logging.getLogger("brain-scheduler")

Registry = dict[str, dict[str, Any]]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SHARED_EXCLUDED_PREFIXES = ("mail_", "contacts_")
SHARED_EXCLUDED_TOOLS = frozenset({"search_knowledge", "ops_summary"})
SHARED_EXCLUDED_ACTION_TYPES = frozenset({
    "mail_archive", "mail_archive_batch", "send_draft", "create_contact", "update_contact", "promote_intake",
})
CHAT_EXCLUDED_TOOLS = frozenset({"push_notify", "send_morning_cards", "handled_cards"})

CHAT_MODEL = "claude-sonnet-5-5"
CHAT_EFFORT = "medium"
CHAT_MAX_TURNS = 20
CHAT_MAX_HELPERS = 2

_PROPERTY_HOSTS = (
    "immobilienscout24", "immoscout24", "immowelt", "immonet",
    "kleinanzeigen", "landingpage.immobilien", "propstack", "drive.google.com",
)
DEEP_DIVE_MODEL = "claude-opus-5-5"
DEEP_DIVE_EFFORT = "max"
DEEP_DIVE_MAX_TURNS = 60


def _is_property_link(text: str | None) -> bool:
    if not text or "http" not in text.lower():
        return False
    low = text.lower()
    return any(host in low for host in _PROPERTY_HOSTS)


def _registry(registry: Registry | None) -> Registry:
    if registry is not None:
        return registry
    from src.orchestrator.registry import AGENT_REGISTRY

    return AGENT_REGISTRY


def _channel(entry: dict[str, Any] | None) -> str | None:
    return (((entry or {}).get("delivery") or {}).get("slack") or {}).get("channel_id") or None


def _chat_agent_of(place: str, registry: Registry) -> str | None:
    names = [name for name, entry in registry.items() if entry.get("chat") == place]
    return names[0] if len(names) == 1 else None


def chat_agent_for_channel(
    channel_id: str | None, registry: Registry | None = None,
) -> str | None:
    """The one chat agent delivered to `channel_id`; None when there is none or several."""
    if not channel_id:
        return None
    names = [
        name for name, entry in _registry(registry).items()
        if entry.get("chat") and _channel(entry) == channel_id
    ]
    return names[0] if len(names) == 1 else None


def place_channel(place: str, registry: Registry | None = None) -> str | None:
    registry = _registry(registry)
    chat_agent = _chat_agent_of(place, registry)
    return _channel(registry[chat_agent]) if chat_agent else None


def area_place(name: str, registry: Registry | None = None) -> str | None:
    """The place an agent belongs to: the `chat` value of the chat agent on its channel.

    A registered agent without Slack delivery belongs to the private place.
    None for a name the registry does not have, or a channel with no chat agent.
    """
    registry = _registry(registry)
    if name not in registry:
        return None
    channel = _channel(registry[name])
    if channel is None:
        return "private"
    chat_agent = chat_agent_for_channel(channel, registry)
    return registry[chat_agent]["chat"] if chat_agent else None


def place_areas(place: str, registry: Registry | None = None) -> list[str]:
    """The areas of `place`, chat agents included, in registry order.

    The shared place has every agent delivered to its channel; the private place
    has every registered agent. A place without a chat agent has none.
    """
    registry = _registry(registry)
    channel = place_channel(place, registry)
    if channel is None:
        return []
    if place == "private":
        return list(registry)
    return [name for name, entry in registry.items() if _channel(entry) == channel]


def _offered(tool: str, *, shared: bool) -> bool:
    bare = tool.removeprefix(BRAIN_TOOL_PREFIX)
    if bare in CHAT_EXCLUDED_TOOLS:
        return False
    return not (
        shared and (bare in SHARED_EXCLUDED_TOOLS or bare.startswith(SHARED_EXCLUDED_PREFIXES))
    )


def _read_root(vault_root: Path, entry: str) -> Path | None:
    path = PurePosixPath(entry)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        log.warning("vault_paths entry %r is not a path inside the vault; ignored", entry)
        return None
    return vault_root.joinpath(*path.parts)


def chat_run_config(
    place: str, *, thread_area: str | None, text: str,
    registry: Registry | None = None, vault_root: Path | None = None,
) -> dict[str, Any]:
    """The configuration of a chat run in `place`: the union of what its areas have.

    The areas are `place_areas(place)`, so a private run gets the union over
    every registered agent. `thread_area` is the area of the thread being
    answered, None for a new message. In the shared place the run may read only
    the shared areas' `vault_paths` and sees only their findings (`read_roots`,
    `areas`); the private place has neither limit.
    """
    registry = _registry(registry)
    chat_agent = _chat_agent_of(place, registry)
    if chat_agent is None:
        raise ValueError(f"no chat agent for place {place!r}")
    shared = place == "shared"
    areas = place_areas(place, registry)

    def union(*keys: str) -> list[str]:
        return list(dict.fromkeys(
            item for name in areas for key in keys for item in registry[name].get(key) or []
        ))

    granted = [*union("tools", "reply_tools"), f"{BRAIN_TOOL_PREFIX}area_notes"]
    tools = [tool for tool in dict.fromkeys(granted) if _offered(tool, shared=shared)]
    vault_paths = union("vault_paths")
    action_types = [
        action for action in union("allowed_action_types")
        if not (shared and action in SHARED_EXCLUDED_ACTION_TYPES)
    ]

    read_roots = None
    if shared:
        if vault_root is None:
            from src.orchestrator.config import VAULT_PATH

            vault_root = VAULT_PATH
        roots = (_read_root(vault_root, entry) for entry in vault_paths)
        read_roots = [root for root in roots if root is not None]

    cfg: dict[str, Any] = {
        "name": chat_agent,
        "model": CHAT_MODEL,
        "effort": CHAT_EFFORT,
        "max_turns": CHAT_MAX_TURNS,
        "tools": tools,
        "allowed_action_types": action_types,
        "vault_paths": vault_paths,
        "max_helpers": CHAT_MAX_HELPERS,
        "read_roots": read_roots,
        "areas": areas if shared else None,
    }
    if thread_area == "housing" and _is_property_link(text):
        cfg.update(model=DEEP_DIVE_MODEL, effort=DEEP_DIVE_EFFORT, max_turns=DEEP_DIVE_MAX_TURNS)
        log.info("housing deep-dive: strongest Claude model + max effort")
    return cfg


def area_index(place: str, registry: Registry | None = None) -> str:
    """One line per area of `place`, for a message that starts a conversation.

    `- <area>: <first line of its area notes>`, or its description when the area
    has no notes, or just its name. The chat agents are the reader, not areas,
    and a system agent is no area to talk about.
    """
    from src.brain.runtime.area_notes import area_notes_text
    from src.brain.runtime.loader import load_all

    registry = _registry(registry)
    loaded = load_all(_REPO_ROOT)
    lines = []
    for area in place_areas(place, registry):
        if registry[area].get("chat") or registry[area].get("kind") == "system":
            continue
        try:
            notes = area_notes_text(area, loaded)
        except Exception as exc:
            log.warning("area notes for %s could not be rendered: %s", area, exc)
            notes = ""
        spec = loaded.agents.get(area)
        summary = (
            next((line.strip() for line in notes.splitlines() if line.strip()), "")
            or str(registry[area].get("description") or "").strip()
            or (spec.description.strip() if spec else "")
        )
        lines.append(f"- {area}: {summary}" if summary else f"- {area}")
    return "\n".join(lines)
