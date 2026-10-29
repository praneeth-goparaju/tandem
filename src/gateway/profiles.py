"""Server-owned tool profiles and caller provenance.

Clients cannot supply or modify these values. The stdio process configuration
selects the profile before any MCP request is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class ProfileConfigurationError(ValueError):
    """Raised when an unsafe or unknown profile is requested."""


READ_TOOLS = frozenset(
    {
        "calendar_list_events",
        "calendar_get_event",
        "reminders_list",
        "reminders_get",
        "mail_search",
        "mail_get_message",
        "mail_list_attachments",
        "mail_get_attachment",
        "contacts_search",
        "contacts_get",
        "garmin_daily",
        "garmin_trends",
        "humans_find",
        "humans_read",
        "humans_due",
        "humans_meetings",
        "humans_open_threads",
        "occasions_scan",
        "get_recent_findings",
        "intake_pending",
        "search_houses",
        "list_watched_searches",
        "ops_summary",
        "follow_ups_list",
    }
)

TRUSTED_WRITE_TOOLS = frozenset(
    {
        "create_calendar_event",
        "update_calendar_event",
        "create_reminder",
        "complete_reminder",
        "create_draft",
        "mail_archive",
        "mail_archive_batch",
        "send_bot_message",
        "follow_ups_open",
        "follow_ups_update",
    }
)

# A scheduled routine may read, record findings, propose actions for the
# backend's approval flow and post to its own fixed channel. It never gets a
# direct-effect tool: every change goes through propose_action.
SCHEDULED_TOOLS = READ_TOOLS | frozenset(
    {
        "store_finding",
        "propose_action",
        "push_notify",
    }
)

PROFILE_TOOLS = {
    "read": READ_TOOLS,
    "trusted": READ_TOOLS | TRUSTED_WRITE_TOOLS,
    "scheduled": SCHEDULED_TOOLS,
}


@dataclass(frozen=True)
class Profile:
    name: str
    tool_names: frozenset[str]
    caller_context: dict[str, object]


def _enabled(raw: str | None) -> bool:
    return (raw or "").strip().casefold() in {"1", "true", "yes", "on"}


def load_profile(environment: Mapping[str, str]) -> Profile:
    """Load one fail-closed profile from an environment mapping."""
    name = environment.get("BRAIN_MCP_PROFILE", "read").strip().casefold()
    if name not in PROFILE_TOOLS:
        expected = ", ".join(sorted(PROFILE_TOOLS))
        raise ProfileConfigurationError(
            f"unknown BRAIN_MCP_PROFILE {name!r}; expected one of: {expected}"
        )

    if name in {"trusted", "scheduled"} and not _enabled(
        environment.get("BRAIN_MCP_ALLOW_WRITES")
    ):
        raise ProfileConfigurationError(f"the {name} profile requires BRAIN_MCP_ALLOW_WRITES=1")

    if name == "read":
        context: dict[str, object] = {
            "agent": "external-mcp-read",
            "area": "interactive-mcp",
            "allowed_action_types": [],
            "direct_action_types": [],
            "is_reply": False,
            "untrusted_input": True,
        }
    elif name == "scheduled":
        # The backend treats a routine as an untrusted-input caller: it reads
        # mail and web pages. Its proposals wait for approval, except the
        # own-device types the backend's policy runs automatically, and
        # push_notify, whose destination is fixed to the caller's area.
        context = {
            "agent": "routine",
            "area": "assistant",
            "allowed_action_types": [
                "complete_reminder",
                "create_calendar_event",
                "create_contact",
                "create_reminder",
                "create_todoist_task",
                "edit_vault_file",
                "edit_vault_files",
                "insert_vault_line",
                "mail_archive",
                "promote_note",
                "push_notification",
                "update_calendar_event",
                "update_contact",
            ],
            "direct_action_types": [],
            "is_reply": False,
            "untrusted_input": True,
        }
    else:
        context = {
            "agent": "external-mcp",
            "area": "interactive-mcp",
            "allowed_action_types": [
                "create_calendar_event",
                "update_calendar_event",
                "create_reminder",
                "complete_reminder",
                "mail_archive",
                "mail_archive_batch",
                "send_bot_message",
            ],
            "direct_action_types": [
                "complete_reminder",
                "mail_archive",
                "mail_archive_batch",
                "send_bot_message",
            ],
            "is_reply": False,
            "untrusted_input": False,
        }

    return Profile(name=name, tool_names=PROFILE_TOOLS[name], caller_context=context)
