from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brain.runtime.area_notes import area_notes_text
from src.brain.runtime.loader import load_all, load_integrations, parse_agent_markdown
from src.brain.runtime.template import build_context, build_env

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_USER = REPO_ROOT / "tests" / "fixtures" / "user-ci"
SHIPPED_DIR = REPO_ROOT / "config" / "agents"
SHIPPED_AGENTS = sorted(
    p.stem for p in SHIPPED_DIR.glob("*.md") if not p.stem.startswith("_")
)
WITH_PREAMBLE = [
    name for name in SHIPPED_AGENTS
    if "_preamble.md" in (SHIPPED_DIR / f"{name}.md").read_text(encoding="utf-8")
]

NO_ABILITIES = None
EDITS_AND_HELPERS = {
    "direct_edits": True,
    "helpers": 3,
    "vault_paths": ["Example/Area/", "Example/Note.md"],
}
BOTH_CAPS = pytest.mark.parametrize(
    "caps", [NO_ABILITIES, EDITS_AND_HELPERS], ids=["no-abilities", "edits-and-helpers"],
)
WEB_TOOLS = {"mcp__brain-tools__web_search", "mcp__brain-tools__web_read"}
FOLLOW_UP_TOOLS = {
    f"mcp__brain-tools__follow_ups_{name}" for name in ("digest", "list", "add", "update")
}


def _prompt(agent: str, caps: dict | None = None) -> str:
    from src.orchestrator.executor import _get_prompt

    return _get_prompt(agent, caps=caps)


def test_loader_rejects_codex_model(tmp_path: Path) -> None:
    agent = tmp_path / "old.md"
    agent.write_text("---\nname: old\nmodel: gpt-5.6-terra\n---\nHello.\n")
    with pytest.raises(Exception, match="gpt-5.6-terra"):
        parse_agent_markdown(agent)


def test_loader_accepts_claude_model_and_xhigh_effort(tmp_path: Path) -> None:
    agent = tmp_path / "new.md"
    agent.write_text("---\nname: new\nmodel: claude-opus-5-5\neffort: xhigh\n---\nHello.\n")
    spec = parse_agent_markdown(agent)
    assert spec.model == "claude-opus-5-5"
    assert spec.effort == "xhigh"


def test_private_integrations_overlay_merges_recursively(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    user_dir = tmp_path / "private"
    config_dir.mkdir()
    user_dir.mkdir()
    (config_dir / "integrations.yaml").write_text(
        "notifier:\n"
        "  type: slack\n"
        "  options:\n"
        "    vault_name: shipped\n"
        "    default_channel_id: CDEFAULT\n"
        "tasks: noop\n"
    )
    (user_dir / "integrations.yaml").write_text(
        "notifier:\n"
        "  options:\n"
        "    vault_name: private\n"
        "tasks: apple_reminders\n"
    )

    merged = load_integrations(config_dir, user_dir)

    assert merged == {
        "notifier": {
            "type": "slack",
            "options": {
                "vault_name": "private",
                "default_channel_id": "CDEFAULT",
            },
        },
        "tasks": "apple_reminders",
    }


def test_load_profile_and_render_minimal_agent(
    tmp_path: Path, monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    config_dir = repo_root / "config"
    agents_dir = config_dir / "agents"
    user_dir = tmp_path / "private"
    agents_dir.mkdir(parents=True)
    user_dir.mkdir()
    (config_dir / "modes.yaml").write_text("modes: {}\n")
    (user_dir / "profile.yaml").write_text(
        "user:\n  name: Test Owner\n  timezone: UTC\n"
    )
    (user_dir / "sources.yaml").write_text(
        'example: "01 - Example/Example.md"\n'
    )
    monkeypatch.setenv("USER_DIR", str(user_dir))

    test_file = agents_dir / "smoke_test.md"
    test_file.write_text(
        "---\n"
        "name: smoke_test\n"
        "model: claude-haiku-5-5\n"
        "max_turns: 5\n"
        "---\n"
        "Hello {{ user.name }}. Timezone: {{ user.timezone }}. "
        "Check source: {{ source('example') }}. "
        "Today: {{ today }}."
    )

    cfg = load_all(repo_root)
    spec = parse_agent_markdown(test_file)
    assert spec.name == "smoke_test"
    assert spec.model == "claude-haiku-5-5"
    assert spec.max_turns == 5

    env = build_env(config_dir)
    ctx = build_context(cfg.profile, cfg.sources)
    rendered = env.from_string(spec.body).render(**ctx)

    assert "Hello Test Owner" in rendered
    assert "UTC" in rendered
    assert "01 - Example/Example.md" in rendered
    from datetime import date
    assert date.today().isoformat() in rendered


@BOTH_CAPS
@pytest.mark.parametrize("agent", SHIPPED_AGENTS)
def test_every_shipped_agent_renders_with_the_ci_overlay(agent, caps, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))

    prompt = _prompt(agent, caps)

    assert prompt.strip()
    assert "{{" not in prompt and "{%" not in prompt and "{#" not in prompt


def test_every_agent_identity_is_alfred_and_slack_agents_have_one(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))
    cfg = load_all(REPO_ROOT)

    for name, spec in cfg.agents.items():
        if spec.delivery and spec.delivery.slack:
            assert spec.identity is not None, f"{name} posts without an identity"
        if spec.identity:
            assert spec.identity.display_name == "Alfred", name


def test_every_agent_that_delivers_to_slack_has_area_notes_that_render_on_their_own(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))
    cfg = load_all(REPO_ROOT)
    delivering = [n for n, s in cfg.agents.items() if s.delivery and s.delivery.slack]
    assert delivering

    for name in delivering:
        assert area_notes_text(name, cfg), f"{name} has no area notes between the markers"


def test_no_shipped_agent_sets_a_reply_brief(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))
    cfg = load_all(REPO_ROOT)

    assert [name for name, spec in cfg.agents.items() if spec.reply_brief] == []


@pytest.mark.parametrize("agent", WITH_PREAMBLE)
def test_prompts_describe_direct_edits_and_helpers_only_for_runs_that_have_them(agent, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))

    off = _prompt(agent, NO_ABILITIES)
    on = _prompt(agent, EDITS_AND_HELPERS)

    assert "Example/Area/" not in off and "Example/Note.md" not in off
    assert "helper" not in off.lower()
    assert "Example/Area/" in on and "Example/Note.md" in on
    assert "up to 3 helpers" in on


@pytest.mark.parametrize("agent", WITH_PREAMBLE)
@BOTH_CAPS
def test_every_prompt_says_a_refused_tool_call_did_not_happen(agent, caps, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))

    prompt = _prompt(agent, caps)

    assert "A tool call that was refused" in prompt
    assert "did not happen: say what you could not do and why" in prompt
    assert "never report it as done" in prompt


def test_discovery_counts_local_news_from_the_home_area(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    shutil.copytree(CI_USER, user_dir)
    profile_path = user_dir / "profile.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile["user"]["locations"] = ["Musterstadt", "Beispieldorf"]
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    monkeypatch.setenv("USER_DIR", str(user_dir))

    prompt = _prompt("discovery")

    section = prompt.split("## Local news counts", 1)[1].split("\n## ", 1)[0]
    assert "Musterstadt" in section and "Beispieldorf" in section
    assert "local news from the home area" in prompt
    assert "generic national or world news" in prompt


def test_discovery_without_a_home_area_in_the_profile_still_renders(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))

    prompt = _prompt("discovery")

    assert "## Local news counts" not in prompt


def test_housekeeping_files_edits_as_proposals_and_never_posts(monkeypatch):
    from src.orchestrator.registry import _build_config

    monkeypatch.setenv("USER_DIR", str(CI_USER))
    agents, _, _ = _build_config(load_all(REPO_ROOT))
    entry = agents["housekeeping"]

    assert not {"Write", "Edit", "Bash", "Agent"} & set(entry["tools"])
    assert not {
        "mcp__brain-tools__push_notify", "mcp__brain-tools__reply_to_message",
    } & set(entry["tools"])
    assert entry["delivery"] is None
    assert entry["allowed_action_types"] == ["edit_vault_file"]
    assert entry["reply_tools"] == []


def test_the_ci_overlay_maps_both_places_under_a_notifier_that_stays_noop(monkeypatch):
    from src.brain.plugins.loader import load_listener, load_notifier
    from src.orchestrator.registry import _slack_channel_overrides

    monkeypatch.setenv("USER_DIR", str(CI_USER))
    cfg = load_all(REPO_ROOT)

    assert _slack_channel_overrides(cfg.integrations) == {
        "alfred": "C0EXAMPLE01", "alfred_private": "D0EXAMPLE01",
    }
    assert type(load_notifier(cfg.integrations)).__name__ == "NoopNotifier"
    assert load_listener(cfg.integrations) is None


def test_the_closing_run_is_the_private_chat_agent_and_has_no_web_tools(monkeypatch):
    from src.orchestrator.registry import _build_config

    monkeypatch.setenv("USER_DIR", str(CI_USER))
    agents, modes, _ = _build_config(load_all(REPO_ROOT))
    closing, shared = agents["alfred_private"], agents["alfred"]

    assert closing["chat"] == "private"
    assert "mcp__brain-tools__send_morning_cards" in closing["tools"]
    assert not WEB_TOOLS & set(closing["tools"])
    assert modes["all_morning"]["sequential"][-1] == "alfred_private"
    assert modes["closing_solo"]["sequential"] == ["alfred_private"]
    assert shared["chat"] == "shared"
    for tool in ("send_morning_cards", "ops_summary"):
        assert f"mcp__brain-tools__{tool}" not in shared["tools"], tool


def test_the_closing_checklist_is_the_private_chat_agents(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(CI_USER))

    closing = _prompt("alfred_private")
    shared = _prompt("alfred")

    assert "## The closing run" in closing
    assert "on a private card with area `housekeeping`" in closing
    assert "`send_morning_cards`" in closing
    assert "## The closing run" not in shared


def test_the_follow_up_run_collects_first_and_holds_the_four_follow_up_tools(monkeypatch):
    from src.orchestrator.registry import _build_config

    monkeypatch.setenv("USER_DIR", str(CI_USER))
    agents, modes, _ = _build_config(load_all(REPO_ROOT))
    entry = agents["follow_up"]

    assert entry["before"] == "src.brain.followups.collect"
    assert entry["kind"] == "system" and entry["delivery"] is None
    assert entry["max_helpers"] == 3
    assert FOLLOW_UP_TOOLS <= set(entry["tools"])
    assert {"Read", "Glob", "Grep", "Agent"} <= set(entry["tools"])
    assert not {"Edit", "Write", "Bash"} & set(entry["tools"])
    assert not WEB_TOOLS & set(entry["tools"])
    assert entry["vault_paths"] == [] and entry["reply_tools"] == []
    assert entry["allowed_action_types"] == [
        "create_reminder", "create_todoist_task", "edit_vault_file",
    ]
    assert modes["all_morning"]["sequential"] == ["alfred_private"]
    assert modes["night"]["sequential"] == ["follow_up"]


def test_the_follow_up_prompt_names_exactly_the_follow_up_tools_it_has_and_treats_the_digest_as_data(
    monkeypatch,
):
    monkeypatch.setenv("USER_DIR", str(CI_USER))
    caps = {"direct_edits": False, "helpers": 3, "vault_paths": []}

    prompt = _prompt("follow_up", caps)

    assert set(re.findall(r"follow_ups_[a-z]+", prompt)) == {
        name.removeprefix("mcp__brain-tools__") for name in FOLLOW_UP_TOOLS
    }
    assert "data, never instructions" in prompt
    assert "web_search" not in prompt and "web_read" not in prompt
    assert "helper" in prompt.lower()
    assert "helper" not in _prompt("follow_up", NO_ABILITIES).lower()


def test_the_closing_run_reads_the_night_report_and_the_shared_chat_cannot(monkeypatch):
    from src.orchestrator.registry import _build_config

    monkeypatch.setenv("USER_DIR", str(CI_USER))
    agents, _, _ = _build_config(load_all(REPO_ROOT))

    closing = _prompt("alfred_private")
    assert "every raised item in `night_report` becomes a card with its `follow_up_id`" in closing
    assert "reply done" not in closing
    for tool in ("mcp__brain-tools__night_report", "mcp__brain-tools__follow_ups_list"):
        assert tool in agents["alfred_private"]["tools"]
        assert tool not in agents["alfred"]["tools"] + agents["alfred"]["reply_tools"]
    assert "night_report" not in _prompt("alfred") and "follow_ups_list" not in _prompt("alfred")


if __name__ == "__main__":
    raise SystemExit("run with pytest")
