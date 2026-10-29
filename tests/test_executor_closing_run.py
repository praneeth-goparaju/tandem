from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.brain.runtime.loader import LoadedConfig
from src.brain.runtime.spec import AgentSpec
from src.brain.runtime.template import build_context, build_env, render_agent
from src.orchestrator import executor, runner
from src.orchestrator.runners.claude_runner import ClaudeRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH = "mcp__brain-tools__push_notify"
POST = "push_notification"
STORE = "mcp__brain-tools__store_finding"
REPLY_LINK = "mcp__brain-tools__mail_reply_link"


def _entry(kind, *, chat=None, tools=(), action_types=(), vault_paths=(), max_helpers=0):
    return {
        "model": "claude-sonnet-5-5", "tools": list(tools), "reply_tools": [],
        "allowed_action_types": list(action_types), "max_turns": 5, "effort": None,
        "kind": kind, "identity": None, "delivery": None, "reply_brief": "", "chat": chat,
        "vault_paths": list(vault_paths), "max_helpers": max_helpers,
    }


REGISTRY = {
    "housekeeping": _entry("system", tools=[STORE], action_types=["edit_vault_file"]),
    "discovery": _entry("service", tools=[STORE, PUSH], action_types=[POST, "create_reminder"]),
    "family": _entry("area_owner", tools=[STORE, PUSH, REPLY_LINK], action_types=[POST]),
    "alfred": _entry("service", chat="shared", tools=[STORE, PUSH], action_types=[POST]),
    "alfred_private": _entry("service", chat="private", tools=[STORE, PUSH],
                             action_types=[POST, "edit_vault_file"]),
}
MORNING = {
    "pre": ["housekeeping"], "parallel": ["discovery", "family"], "sequential": ["alfred_private"],
}
MORNING_WITH_TRIAGE = {
    "parallel": ["discovery"], "sequential": ["family", "alfred_private"],
}


@pytest.mark.parametrize(("run_config", "closing"), [
    pytest.param(MORNING, "alfred_private", id="a-mode-ending-with-the-private-chat-agent"),
    pytest.param(MORNING_WITH_TRIAGE, "alfred_private", id="a-sequential-agent-before-it"),
    pytest.param(
        {"parallel": [], "sequential": ["alfred_private"]}, "alfred_private",
        id="that-agent-alone",
    ),
    pytest.param({"parallel": ["discovery"], "sequential": []}, None, id="no-sequential-agent"),
    pytest.param({"parallel": [], "sequential": ["discovery"]}, None, id="last-agent-has-no-chat"),
    pytest.param(
        {"parallel": ["family"], "sequential": ["alfred"]}, None, id="shared-chat-does-not-close",
    ),
    pytest.param(
        {"parallel": [], "sequential": ["alfred_private", "discovery"]}, None,
        id="only-the-last-closes",
    ),
    pytest.param(
        {"parallel": ["alfred_private"], "sequential": []}, None, id="parallel-does-not-close",
    ),
    pytest.param({"parallel": [], "sequential": ["ghost"]}, None, id="unknown-agent"),
])
def test_closing_agent_is_detected_from_the_mode(monkeypatch, run_config, closing):
    monkeypatch.setattr(runner, "AGENT_REGISTRY", REGISTRY)

    assert runner._runtime_context("some_mode", run_config)["closing_agent"] == closing


def test_runtime_context_still_lists_the_agents_the_mode_dispatches(monkeypatch):
    monkeypatch.setattr(runner, "AGENT_REGISTRY", REGISTRY)

    ctx = runner._runtime_context("all_morning", MORNING)

    assert ctx["active_mode"] == "all_morning"
    assert ctx["expected_agents"] == ["housekeeping", "discovery", "family", "alfred_private"]
    assert ctx["expected_area_owners"] == ["family"]


def _fake_claude(monkeypatch, modes):
    seen: dict[str, dict] = {}

    async def fake_run(self, agent_cfg, trigger, context):
        seen[context["agent_name"]] = agent_cfg
        return {"agent": context["agent_name"], "status": "success"}

    monkeypatch.setattr(ClaudeRunner, "run", fake_run)
    monkeypatch.setattr(executor, "_get_prompt", lambda *args, **kwargs: "prompt")
    for module in (runner, executor):
        monkeypatch.setattr(module, "AGENT_REGISTRY", REGISTRY)
    monkeypatch.setattr(runner, "RUN_MODES", modes)
    monkeypatch.setattr(runner, "_check_cost_anomaly", lambda *args: None)
    monkeypatch.setattr(runner, "_log_daily_rollup", lambda: None)
    return seen


@pytest.mark.parametrize(("run_config", "agents"), [
    pytest.param(MORNING, {"housekeeping", "discovery", "family", "alfred_private"}, id="morning"),
    pytest.param(MORNING_WITH_TRIAGE, {"discovery", "family", "alfred_private"}, id="with-triage"),
])
def test_area_runs_lose_push_notify_when_a_closing_run_follows(monkeypatch, run_config, agents):
    before = {name: list(entry["allowed_action_types"]) for name, entry in REGISTRY.items()}
    seen = _fake_claude(monkeypatch, {"all_morning": run_config})

    asyncio.run(runner.run_orchestrated("all_morning"))

    assert set(seen) == agents
    for name in agents - {"alfred_private"}:
        assert PUSH not in seen[name]["tools"], name
        assert REPLY_LINK not in seen[name]["tools"], name
        assert POST not in seen[name]["allowed_action_types"], name
    assert STORE in seen["discovery"]["tools"]
    assert STORE in seen["family"]["tools"]
    assert REPLY_LINK in REGISTRY["family"]["tools"]
    assert PUSH in seen["alfred_private"]["tools"]
    assert PUSH in REGISTRY["family"]["tools"]
    assert seen["discovery"]["allowed_action_types"] == ["create_reminder"]
    assert seen["family"]["allowed_action_types"] == []
    if "housekeeping" in agents:
        assert seen["housekeeping"]["allowed_action_types"] == ["edit_vault_file"]
    assert seen["alfred_private"]["allowed_action_types"] == [POST, "edit_vault_file"]
    assert {n: list(e["allowed_action_types"]) for n, e in REGISTRY.items()} == before


def test_the_run_summary_prints_an_agent_skipped_by_its_switch_as_skipped(monkeypatch, capsys):
    mode = {"parallel": [], "sequential": ["follow_up", "alfred_private"]}
    seen = _fake_claude(monkeypatch, {"all_morning": mode})
    registry = {**REGISTRY, "follow_up": _entry("system", tools=[STORE])}
    for module in (runner, executor):
        monkeypatch.setattr(module, "AGENT_REGISTRY", registry)
    monkeypatch.setenv("BRAIN_FOLLOWUPS", "0")

    asyncio.run(runner.run_orchestrated("all_morning"))

    output = capsys.readouterr().out
    skipped_line = next(line for line in output.splitlines() if "follow_up" in line)
    assert "skipped" in skipped_line and "\u2717" not in skipped_line
    assert "1/1 agents succeeded (1 skipped)" in output
    assert set(seen) == {"alfred_private"}


def test_an_area_run_cannot_post_through_propose_action_either(monkeypatch, isolated_db):
    from src.brain.actions import queue
    from src.brain.mcp.caller_context import caller_scope, parse_caller_context
    from src.brain.mcp.tools.actions import propose_action

    seen = _fake_claude(monkeypatch, {"all_morning": MORNING})
    asyncio.run(runner.run_orchestrated("all_morning"))

    def caller_of(name):
        context = ClaudeRunner._caller_context(seen[name], name, "", {}, is_reply=False)
        return parse_caller_context(context)

    assert POST in caller_of("alfred_private").allowed_action_types
    for name in ("discovery", "family"):
        assert POST not in caller_of(name).allowed_action_types, name

    with caller_scope(caller_of("discovery")):
        out = asyncio.run(propose_action({
            "type": POST,
            "payload": {"title": "x", "message": "y"},
            "rationale": "an area run trying to post by the action route",
            "risk": "auto",
        }))
    assert out["is_error"]
    assert "not allowed for agent 'discovery'" in out["content"][0]["text"]
    assert queue.list_by_status("pending") == []


@pytest.mark.parametrize(("name", "run_config", "agents"), [
    pytest.param("discovery_solo", {"parallel": [], "sequential": ["discovery"]}, ["discovery"],
                 id="solo-run-of-an-area"),
    pytest.param("scout", {"parallel": ["discovery", "family"], "sequential": []},
                 ["discovery", "family"], id="sweep-without-a-sequential-agent"),
    pytest.param("shared_last", {"parallel": ["family"], "sequential": ["alfred"]},
                 ["family", "alfred"], id="last-agent-is-the-shared-chat-agent"),
])
def test_a_mode_without_a_closing_run_keeps_push_notify(monkeypatch, name, run_config, agents):
    seen = _fake_claude(monkeypatch, {name: run_config})

    asyncio.run(runner.run_orchestrated(name))

    assert set(seen) == set(agents)
    for agent in agents:
        assert PUSH in seen[agent]["tools"], agent
        assert seen[agent]["allowed_action_types"] == REGISTRY[agent]["allowed_action_types"]
    if "family" in agents:
        assert REPLY_LINK in seen["family"]["tools"]


def test_a_single_agent_run_keeps_push_notify(monkeypatch):
    seen = _fake_claude(monkeypatch, {})

    asyncio.run(runner.run_orchestrated("family"))

    assert PUSH in seen["family"]["tools"]
    assert POST in seen["family"]["allowed_action_types"]


def test_caps_follow_grants_and_switches():
    entry = _entry(
        "service", tools=["Read", "Edit", "Agent", STORE],
        vault_paths=["05 - Health/"], max_helpers=3,
    )

    assert build_context({}, {})["caps"] == {
        "direct_edits": False, "helpers": 0, "vault_paths": [],
    }
    given = {"direct_edits": True, "helpers": 2, "vault_paths": ["Notes/"]}
    assert build_context({}, {}, caps=given)["caps"] == given

    on = {"direct_edits": True, "helpers": 3, "vault_paths": ["05 - Health/"]}
    assert executor._caps(entry, {}) == on
    assert executor._caps(entry, {"BRAIN_DIRECT_EDITS": "1", "BRAIN_HELPERS": "on"}) == on
    assert executor._caps(entry, {"BRAIN_DIRECT_EDITS": "0"}) == {**on, "direct_edits": False}
    assert executor._caps(entry, {"BRAIN_HELPERS": "off"}) == {**on, "helpers": 0}
    assert executor._caps(entry, {"BRAIN_DIRECT_EDITS": "no", "BRAIN_HELPERS": "false"}) == {
        "direct_edits": False, "helpers": 0, "vault_paths": ["05 - Health/"],
    }


def test_caps_need_the_grant_in_the_first_place():
    base = {"vault_paths": ["A/"], "max_helpers": 2}

    nothing = executor._caps({**base, "tools": ["Read", "Glob", "Grep", STORE]}, {})
    assert nothing == {"direct_edits": False, "helpers": 0, "vault_paths": ["A/"]}
    assert executor._caps({**base, "tools": ["Write"]}, {})["direct_edits"] is True
    assert executor._caps({**base, "tools": ["Edit"]}, {})["direct_edits"] is True
    assert executor._caps({**base, "tools": ["Agent"], "max_helpers": 0}, {})["helpers"] == 0
    assert executor._caps({**base, "tools": ["Agent"]}, {})["helpers"] == 2
    assert executor._caps({"tools": ["Agent", "Edit"]}, {}) == {
        "direct_edits": True, "helpers": 0, "vault_paths": [],
    }


def test_prompt_is_rendered_with_the_caps_of_the_run(monkeypatch):
    body = (
        "{% if caps.direct_edits %}You may edit {{ caps.vault_paths | join(', ') }}.\n{% endif %}"
        "{% if caps.helpers %}You may start {{ caps.helpers }} helpers.\n{% endif %}"
        "Done."
    )
    cfg = LoadedConfig(repo_root=REPO_ROOT, agents={"worker": AgentSpec(name="worker", body=body)})
    registry = {
        "worker": _entry("service", tools=["Read", "Edit", "Agent"],
                         vault_paths=["Notes/", "Plans/"], max_helpers=2),
    }
    monkeypatch.setattr("src.brain.runtime.loader.load_all", lambda root: cfg)
    monkeypatch.setattr("src.brain.runtime.vault_instructions.render_section", lambda *a, **k: "")
    monkeypatch.setattr(executor, "AGENT_REGISTRY", registry)
    for name in ("BRAIN_DIRECT_EDITS", "BRAIN_HELPERS"):
        monkeypatch.delenv(name, raising=False)
    prompts: list[str] = []

    async def fake_run(self, agent_cfg, trigger, context):
        prompts.append(context["system_prompt"])
        return {"agent": "worker", "status": "success"}

    monkeypatch.setattr(ClaudeRunner, "run", fake_run)

    asyncio.run(executor.run_single_agent("worker"))
    assert prompts[-1] == "You may edit Notes/, Plans/.\nYou may start 2 helpers.\nDone."

    monkeypatch.setenv("BRAIN_HELPERS", "0")
    asyncio.run(executor.run_single_agent("worker"))
    assert prompts[-1] == "You may edit Notes/, Plans/.\nDone."

    monkeypatch.setenv("BRAIN_DIRECT_EDITS", "off")
    asyncio.run(executor.run_single_agent("worker"))
    assert prompts[-1] == "Done."


def test_templates_can_read_caps_when_the_caller_gave_none():
    env = build_env(REPO_ROOT / "config")
    body = "{% if caps.direct_edits %}edit{% endif %}{% if caps.helpers %}help{% endif %}ok"

    assert render_agent(env, body, build_context({}, {})) == "ok"
