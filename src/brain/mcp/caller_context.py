from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCaller:
    agent: str
    area: str | None = None
    allowed_action_types: frozenset[str] = frozenset()
    direct_action_types: frozenset[str] = frozenset()
    is_reply: bool = False
    untrusted_input: bool = False
    web_query_terms: frozenset[str] = frozenset()
    outside_content: bool = False
    areas: frozenset[str] | None = None
    vault_paths: tuple[str, ...] | None = None
    private_place: bool = False
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    slack_sender: str | None = None


_SLACK_VALUE = re.compile(r"[A-Za-z0-9.:_-]{1,64}")

_CURRENT_CALLER: ContextVar[ToolCaller | None] = ContextVar(
    "brain_tool_caller", default=None
)


def current_caller() -> ToolCaller | None:
    return _CURRENT_CALLER.get()


def parse_caller_context(value: object) -> ToolCaller | None:
    if not isinstance(value, Mapping):
        return None
    agent = value.get("agent")
    area = value.get("area")
    raw_action_types = value.get("allowed_action_types")
    raw_direct_types = value.get("direct_action_types", [])
    is_reply = value.get("is_reply")
    untrusted_input = value.get("untrusted_input")
    raw_web_terms = value.get("web_query_terms", [])
    outside_content = value.get("outside_content", False)
    raw_areas = value.get("areas")
    raw_vault_paths = value.get("vault_paths")
    slack_channel, slack_thread_ts = value.get("slack_channel"), value.get("slack_thread_ts")
    slack_sender = value.get("slack_sender")
    for slack_value in (slack_channel, slack_thread_ts, slack_sender):
        if slack_value is not None and (
            not isinstance(slack_value, str) or not _SLACK_VALUE.fullmatch(slack_value)
        ):
            return None
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 100:
        return None
    if area is not None and (not isinstance(area, str) or len(area) > 100):
        return None
    if not isinstance(raw_action_types, list) or len(raw_action_types) > 100:
        return None
    if not isinstance(raw_direct_types, list) or len(raw_direct_types) > 100:
        return None
    if not isinstance(is_reply, bool) or not isinstance(untrusted_input, bool):
        return None
    if not isinstance(outside_content, bool):
        return None
    if raw_areas is not None and (not isinstance(raw_areas, list) or len(raw_areas) > 100):
        return None
    if raw_vault_paths is not None and (
        not isinstance(raw_vault_paths, list)
        or len(raw_vault_paths) > 100
        or any(not isinstance(item, str) or not item or "\x00" in item for item in raw_vault_paths)
    ):
        return None
    if not isinstance(raw_web_terms, list) or len(raw_web_terms) > 500:
        return None
    if any(
        not isinstance(item, str) or not item.strip() or len(item) > 100
        for item in raw_action_types
    ):
        return None
    if any(
        not isinstance(item, str) or not item.strip() or len(item) > 100
        for item in raw_direct_types
    ):
        return None
    if raw_areas is not None and any(
        not isinstance(item, str) or not item.strip() or len(item) > 100
        for item in raw_areas
    ):
        return None
    normalized_action_types = frozenset(item.strip() for item in raw_action_types)
    normalized_direct_types = frozenset(item.strip() for item in raw_direct_types)
    if not normalized_direct_types.issubset(normalized_action_types):
        return None
    if any(
        not isinstance(item, str) or not item or len(item) > 64
        for item in raw_web_terms
    ):
        return None
    return ToolCaller(
        agent=agent.strip(),
        area=area.strip() if area else None,
        allowed_action_types=normalized_action_types,
        direct_action_types=normalized_direct_types,
        is_reply=is_reply,
        untrusted_input=untrusted_input,
        web_query_terms=frozenset(item.casefold() for item in raw_web_terms),
        outside_content=outside_content,
        areas=None if raw_areas is None else frozenset(item.strip() for item in raw_areas),
        vault_paths=None if raw_vault_paths is None else tuple(raw_vault_paths),
        private_place="areas" in value and raw_areas is None,
        slack_channel=slack_channel,
        slack_thread_ts=slack_thread_ts,
        slack_sender=slack_sender,
    )


def require_current_caller() -> ToolCaller:
    """Return provider-authenticated provenance or fail closed."""
    caller = current_caller()
    if caller is None:
        raise PermissionError("trusted caller context is required")
    return caller


@contextmanager
def caller_scope(caller: ToolCaller | None) -> Iterator[None]:
    token = _CURRENT_CALLER.set(caller)
    try:
        yield
    finally:
        _CURRENT_CALLER.reset(token)
