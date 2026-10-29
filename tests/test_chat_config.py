from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.brain.actions.types import ActionType
from src.brain.mcp.caller_context import parse_caller_context
from src.brain.runtime.loader import LoadedConfig
from src.brain.runtime.spec import AgentSpec
from src.orchestrator import chat

BRAIN = "mcp__brain-tools__"


def _agent(channel, *, place=None, tools=(), reply_tools=(), actions=(), paths=(), kind=None):
    return {
        "chat": place,
        "kind": kind,
        "delivery": {"slack": {"channel_id": channel}} if channel else None,
        "tools": list(tools),
        "reply_tools": list(reply_tools),
        "allowed_action_types": list(actions),
        "vault_paths": list(paths),
    }


REGISTRY = {
    "alfred": _agent(
        "CSHARED", place="shared",
        tools=["Read", f"{BRAIN}reply_to_message", f"{BRAIN}propose_action"],
        actions=["push_notification"],
    ),
    "alfred_private": _agent(
        "DPRIV", place="private",
        tools=["Read", f"{BRAIN}reply_to_message", f"{BRAIN}send_morning_cards"],
    ),
    "family": _agent(
        "CSHARED",
        tools=["Read", "Grep", f"{BRAIN}mail_search", f"{BRAIN}contacts_get",
               f"{BRAIN}search_knowledge", f"{BRAIN}push_notify",
               f"{BRAIN}calendar_list_events"],
        reply_tools=[f"{BRAIN}reply_to_message", f"{BRAIN}create_reminder"],
        actions=["create_reminder", "push_notification"],
        paths=["Family/"],
    ),
    "housing": _agent(
        "CSHARED",
        tools=[f"{BRAIN}web_search", f"{BRAIN}mail_get_message"],
        actions=["edit_vault_file"],
    ),
    "health": _agent(
        "DPRIV",
        tools=["Read", "Edit", f"{BRAIN}garmin_daily", f"{BRAIN}mail_search",
               f"{BRAIN}push_notify"],
        reply_tools=[f"{BRAIN}contacts_search"],
        actions=["edit_vault_file"],
        paths=["Health/"],
    ),
    "quiet": _agent(None, tools=["Read"], kind="system"),
}


def _config(place, *, thread_area=None, text="hello", registry=None, **kwargs):
    return chat.chat_run_config(
        place, thread_area=thread_area, text=text, registry=registry or REGISTRY, **kwargs,
    )


def test_chat_agent_for_channel_and_area_place():
    assert chat.chat_agent_for_channel("CSHARED", REGISTRY) == "alfred"
    assert chat.chat_agent_for_channel("DPRIV", REGISTRY) == "alfred_private"
    assert chat.chat_agent_for_channel("CELSEWHERE", REGISTRY) is None
    assert chat.chat_agent_for_channel(None, REGISTRY) is None
    twice = {**REGISTRY, "alfred_two": _agent("CSHARED", place="shared")}
    assert chat.chat_agent_for_channel("CSHARED", twice) is None

    assert chat.area_place("alfred", REGISTRY) == "shared"
    assert chat.area_place("family", REGISTRY) == "shared"
    assert chat.area_place("health", REGISTRY) == "private"
    assert chat.area_place("quiet", REGISTRY) == "private"
    assert chat.area_place("nobody", REGISTRY) is None
    assert chat.area_place("family", twice) is None
    orphan = {**REGISTRY, "career": _agent("CCAREER")}
    assert chat.area_place("career", orphan) is None


def test_place_areas_lists_the_shared_channel_and_every_agent_for_the_private_place():
    assert chat.place_areas("shared", REGISTRY) == ["alfred", "family", "housing"]
    assert chat.place_areas("private", REGISTRY) == list(REGISTRY)
    assert chat.place_areas("elsewhere", REGISTRY) == []


def test_shared_config_excludes_mail_contacts_knowledge_and_push_and_confines_reads():
    cfg = _config("shared", vault_root=Path("/v"))

    assert cfg["tools"] == [
        "Read", f"{BRAIN}reply_to_message", f"{BRAIN}propose_action", "Grep",
        f"{BRAIN}calendar_list_events", f"{BRAIN}create_reminder", f"{BRAIN}web_search",
        f"{BRAIN}area_notes",
    ]
    assert cfg["read_roots"] == [Path("/v/Family")]
    assert cfg["areas"] == ["alfred", "family", "housing"]
    assert cfg["name"] == "alfred"
    assert cfg["vault_paths"] == ["Family/"]
    assert cfg["allowed_action_types"] == [
        "push_notification", "create_reminder", "edit_vault_file",
    ]
    assert (cfg["model"], cfg["effort"], cfg["max_turns"], cfg["max_helpers"]) == (
        "claude-sonnet-5-5", "medium", 20, 2,
    )


def test_private_config_has_every_area_and_no_read_roots():
    cfg = _config("private")

    assert cfg["name"] == "alfred_private"
    assert cfg["read_roots"] is None
    assert cfg["areas"] is None
    for tool in (
        f"{BRAIN}mail_search", f"{BRAIN}contacts_get", f"{BRAIN}search_knowledge",
        f"{BRAIN}calendar_list_events", f"{BRAIN}create_reminder",
        f"{BRAIN}web_search", f"{BRAIN}mail_get_message",
        f"{BRAIN}garmin_daily", f"{BRAIN}contacts_search", "Edit", "Grep",
        f"{BRAIN}area_notes",
    ):
        assert tool in cfg["tools"], tool
    for gone in ("push_notify", "send_morning_cards"):
        assert f"{BRAIN}{gone}" not in cfg["tools"], gone
    assert cfg["vault_paths"] == ["Family/", "Health/"]
    assert cfg["allowed_action_types"] == [
        "push_notification", "create_reminder", "edit_vault_file",
    ]
    assert (cfg["model"], cfg["effort"], cfg["max_turns"], cfg["max_helpers"]) == (
        "claude-sonnet-5-5", "medium", 20, 2,
    )


def test_shared_config_leaves_out_action_types_that_reach_mail_contacts_or_intake():
    snapshotting = [
        "mail_archive", "send_draft", "create_contact", "update_contact", "promote_intake",
    ]
    registry = {
        **REGISTRY,
        "family": {**REGISTRY["family"], "allowed_action_types": ["create_reminder", *snapshotting]},
        "health": {**REGISTRY["health"], "allowed_action_types": ["edit_vault_file", "mail_archive"]},
    }

    shared = _config("shared", registry=registry)
    private = _config("private", registry=registry)

    assert shared["allowed_action_types"] == [
        "push_notification", "create_reminder", "edit_vault_file",
    ]
    assert shared["vault_paths"] == ["Family/"]
    assert "mail_archive" in private["allowed_action_types"]
    assert chat.SHARED_EXCLUDED_ACTION_TYPES <= {item.value for item in ActionType}


def test_parse_caller_context_validates_vault_paths():
    envelope = {
        "agent": "alfred", "area": "family", "allowed_action_types": [],
        "is_reply": True, "untrusted_input": False, "areas": ["alfred", "family"],
    }

    limited = parse_caller_context({**envelope, "vault_paths": ["Family/", "Plans/Weekend.md"]})
    assert limited is not None
    assert limited.vault_paths == ("Family/", "Plans/Weekend.md")
    for unlimited in (envelope, {**envelope, "vault_paths": None}):
        caller = parse_caller_context(unlimited)
        assert caller is not None and caller.vault_paths is None
    for bad in ("Family/", [""], ["Fam" + chr(0) + "ily/"], [7], ["Family/"] * 101):
        assert parse_caller_context({**envelope, "vault_paths": bad}) is None, bad


def test_deep_dive_switches_model_in_housing_thread_only():
    link = "https://www.immobilienscout24.de/expose/999"

    deep = _config("shared", thread_area="housing", text=link)
    assert (deep["model"], deep["effort"], deep["max_turns"]) == ("claude-opus-5-5", "max", 60)

    for thread_area, text in (("family", link), (None, link), ("housing", "anything new?")):
        light = _config("shared", thread_area=thread_area, text=text)
        assert (light["model"], light["effort"], light["max_turns"]) == (
            "claude-sonnet-5-5", "medium", 20,
        )
    assert deep["tools"] == light["tools"]
    assert deep["max_helpers"] == 2


def test_read_roots_are_folders_and_files_inside_the_vault():
    registry = {
        **REGISTRY,
        "family": {
            **REGISTRY["family"],
            "vault_paths": [
                "Family/", "Plans/Weekend.md", "", "/etc/", "//etc/", "../Other/", "A/../B/",
            ],
        },
    }

    cfg = _config("shared", registry=registry, vault_root=Path("/v"))

    assert cfg["read_roots"] == [Path("/v/Family"), Path("/v/Plans/Weekend.md")]


def test_read_roots_default_to_the_configured_vault():
    from src.orchestrator.config import VAULT_PATH

    assert _config("shared")["read_roots"] == [VAULT_PATH / "Family"]


def test_chat_does_not_load_the_registry_when_it_is_imported():
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "USER_DIR": str(root / "tests" / "fixtures" / "user-ci")}
    probe = "import sys, src.orchestrator.chat; sys.exit('src.orchestrator.registry' in sys.modules)"

    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=root, env=env, capture_output=True, text=True,
    )

    assert done.returncode == 0, done.stderr


def test_a_place_without_a_chat_agent_has_no_config():
    with pytest.raises(ValueError, match="elsewhere"):
        _config("elsewhere")


def test_area_index_has_one_line_per_area(monkeypatch, tmp_path):
    notes = (
        "Checklist before.\n"
        "{# area-notes #}\n\nFamily, school and the weekend plans.\nMore detail.\n"
        "{# /area-notes #}\n"
        "Checklist after.\n"
    )
    loaded = LoadedConfig(
        repo_root=tmp_path,
        agents={
            "family": AgentSpec(name="family", body=notes),
            "alfred": AgentSpec(name="alfred", body="{# area-notes #}Alfred.{# /area-notes #}"),
        },
        profile={},
    )
    monkeypatch.setattr("src.brain.runtime.loader.load_all", lambda _root: loaded)
    registry = {
        **REGISTRY,
        "housing": {**REGISTRY["housing"], "description": "House hunt"},
        "career": _agent("CSHARED"),
    }

    assert chat.area_index("shared", registry).splitlines() == [
        "- family: Family, school and the weekend plans.",
        "- housing: House hunt",
        "- career",
    ]
    assert chat.area_index("private", registry).splitlines() == [
        "- family: Family, school and the weekend plans.",
        "- housing: House hunt",
        "- health",
        "- career",
    ]


def test_a_chat_never_offers_the_closing_runs_fleet_tools():
    closing = {
        **REGISTRY,
        "alfred": {
            **REGISTRY["alfred"],
            "tools": [*REGISTRY["alfred"]["tools"], f"{BRAIN}ops_summary",
                      f"{BRAIN}send_morning_cards"],
        },
        "alfred_private": {
            **REGISTRY["alfred_private"],
            "tools": [*REGISTRY["alfred_private"]["tools"], f"{BRAIN}ops_summary"],
        },
    }

    shared = _config("shared", registry=closing)["tools"]
    private = _config("private", registry=closing)["tools"]

    assert f"{BRAIN}ops_summary" not in shared
    assert f"{BRAIN}send_morning_cards" not in shared
    assert f"{BRAIN}send_morning_cards" not in private
    assert f"{BRAIN}ops_summary" in private

