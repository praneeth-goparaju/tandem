from __future__ import annotations

import pytest

from src.gateway.profiles import (
    READ_TOOLS,
    TRUSTED_WRITE_TOOLS,
    ProfileConfigurationError,
    load_profile,
)


def test_read_is_the_safe_default() -> None:
    profile = load_profile({})

    assert profile.name == "read"
    assert profile.tool_names == READ_TOOLS
    assert profile.caller_context["allowed_action_types"] == []
    assert profile.caller_context["direct_action_types"] == []
    assert profile.caller_context["untrusted_input"] is True


def test_trusted_profile_requires_explicit_write_opt_in() -> None:
    with pytest.raises(ProfileConfigurationError, match="ALLOW_WRITES"):
        load_profile({"BRAIN_MCP_PROFILE": "trusted"})


def test_trusted_profile_is_exactly_34_working_tools() -> None:
    profile = load_profile({"BRAIN_MCP_PROFILE": "trusted", "BRAIN_MCP_ALLOW_WRITES": "1"})

    assert len(READ_TOOLS) == 24
    assert len(TRUSTED_WRITE_TOOLS) == 10
    assert READ_TOOLS.isdisjoint(TRUSTED_WRITE_TOOLS)
    assert len(profile.tool_names) == 34
    assert {"follow_ups_list", "follow_ups_open", "follow_ups_update"} <= profile.tool_names
    assert profile.caller_context["direct_action_types"] == [
        "complete_reminder",
        "mail_archive",
        "mail_archive_batch",
        "send_bot_message",
    ]
    assert "send_bot_message" not in READ_TOOLS


def test_reply_only_people_writes_are_not_advertised() -> None:
    profile = load_profile({"BRAIN_MCP_PROFILE": "trusted", "BRAIN_MCP_ALLOW_WRITES": "1"})

    assert profile.tool_names.isdisjoint(
        {"humans_log", "humans_add_person", "humans_resolve_thread"}
    )


def test_unknown_profile_fails_closed() -> None:
    with pytest.raises(ProfileConfigurationError, match="unknown"):
        load_profile({"BRAIN_MCP_PROFILE": "everything"})


def test_scheduled_profile_proposes_but_never_executes_directly() -> None:
    from src.gateway.profiles import SCHEDULED_TOOLS

    profile = load_profile({"BRAIN_MCP_PROFILE": "scheduled", "BRAIN_MCP_ALLOW_WRITES": "1"})

    assert profile.name == "scheduled"
    assert profile.tool_names == SCHEDULED_TOOLS
    assert len(profile.tool_names) == 27
    assert READ_TOOLS < profile.tool_names
    assert profile.tool_names.isdisjoint(TRUSTED_WRITE_TOOLS)
    assert {"store_finding", "propose_action", "push_notify"} <= profile.tool_names

    context = profile.caller_context
    assert context["agent"] == "routine"
    assert context["area"] == "assistant"
    assert context["direct_action_types"] == []
    assert context["is_reply"] is False
    assert context["untrusted_input"] is True
    assert context["allowed_action_types"] == [
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
    ]
    assert "send_bot_message" not in context["allowed_action_types"]
    assert "send_draft" not in context["allowed_action_types"]


def test_scheduled_profile_requires_explicit_write_opt_in() -> None:
    with pytest.raises(ProfileConfigurationError, match="ALLOW_WRITES"):
        load_profile({"BRAIN_MCP_PROFILE": "scheduled"})


def test_only_the_scheduled_profile_may_propose_a_vault_line_insertion() -> None:
    read = load_profile({})
    trusted = load_profile({"BRAIN_MCP_PROFILE": "trusted", "BRAIN_MCP_ALLOW_WRITES": "1"})
    scheduled = load_profile({"BRAIN_MCP_PROFILE": "scheduled", "BRAIN_MCP_ALLOW_WRITES": "1"})

    assert "insert_vault_line" not in read.caller_context["allowed_action_types"]
    assert "insert_vault_line" not in trusted.caller_context["allowed_action_types"]
    assert "insert_vault_line" not in trusted.caller_context["direct_action_types"]
    assert "insert_vault_line" in scheduled.caller_context["allowed_action_types"]
    assert "insert_vault_line" not in scheduled.caller_context["direct_action_types"]


def test_every_offered_tool_exists_in_the_brain() -> None:
    from src.brain.mcp.server import load_brain_tools

    registered = {tool.name for tool in load_brain_tools()}
    for name in ("read", "trusted", "scheduled"):
        env = {"BRAIN_MCP_PROFILE": name, "BRAIN_MCP_ALLOW_WRITES": "1"}
        assert load_profile(env).tool_names <= registered, name
