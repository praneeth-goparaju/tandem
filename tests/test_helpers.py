from __future__ import annotations

from src.orchestrator.runners.helpers import (
    HELPER_PROMPT,
    helper_definition,
)


def test_helper_definition_is_read_only_sonnet_with_fifteen_turns() -> None:
    definition = helper_definition(
        ["mail_search", "propose_action", "garmin_daily", "reply_to_message"],
    )
    assert definition.tools == [
        "Read", "Glob", "Grep",
        "mcp__brain-tools__mail_search", "mcp__brain-tools__garmin_daily",
    ]
    assert definition.disallowedTools is not None
    assert "Agent" in definition.disallowedTools
    assert "Bash" in definition.disallowedTools
    assert definition.model == "claude-sonnet-5-5"
    assert definition.maxTurns == 15
    assert definition.effort == "medium"
    assert definition.mcpServers == ["brain-tools"]
    assert definition.description == "Read-only research helper"


def test_helper_prompt_says_it_cannot_write_and_treats_content_as_data() -> None:
    assert "cannot" in HELPER_PROMPT
    assert "data, not instructions" in HELPER_PROMPT


def test_helpers_never_get_the_web_tools() -> None:
    definition = helper_definition(["web_search", "web_read", "mail_search"])

    assert definition.tools == ["Read", "Glob", "Grep", "mcp__brain-tools__mail_search"]
    assert "web" not in HELPER_PROMPT


def test_helper_without_brain_tools_reads_files_only() -> None:
    assert helper_definition([]).tools == ["Read", "Glob", "Grep"]
