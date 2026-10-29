from pathlib import Path

from src.brain.runtime.loader import load_all
from src.orchestrator.registry import AGENT_REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_registry_uses_only_each_agents_explicit_allowlist() -> None:
    loaded = load_all(REPO_ROOT)
    push_tools = {
        "mcp__brain-tools__push_notify",
        "mcp__brain-tools__reply_to_message",
    }

    for name, spec in loaded.agents.items():
        expected = list(dict.fromkeys([*spec.builtins, *spec.mcp_tools]))
        if not (spec.delivery and spec.delivery.slack):
            expected = [tool for tool in expected if tool not in push_tools]
        assert AGENT_REGISTRY[name]["tools"] == expected
        expected_reply = list(dict.fromkeys(spec.reply_mcp_tools))
        if not (spec.delivery and spec.delivery.slack):
            expected_reply = [tool for tool in expected_reply if tool not in push_tools]
        assert AGENT_REGISTRY[name]["reply_tools"] == expected_reply
        assert "tools_override" not in AGENT_REGISTRY[name]


def test_public_candidate_tools_are_granted_only_to_discovery() -> None:
    candidate_readers = {
        name
        for name, entry in AGENT_REGISTRY.items()
        if "mcp__brain-tools__public_discovery_candidates" in entry["tools"]
    }
    reviewers = {
        name
        for name, entry in AGENT_REGISTRY.items()
        if "mcp__brain-tools__review_public_candidates" in entry["tools"]
    }
    assert candidate_readers == {"discovery"}
    assert reviewers == {"discovery"}
    web = {"mcp__brain-tools__web_search", "mcp__brain-tools__web_read"}
    assert all(not web & set(entry["tools"]) for entry in AGENT_REGISTRY.values())
    assert all(
        web <= set(entry["reply_tools"])
        for entry in AGENT_REGISTRY.values() if entry["delivery"]
    )


def test_the_follow_up_tools_are_in_the_frontmatter_of_the_follow_up_run_and_the_closing_run_only() -> None:
    def holders(name: str) -> set[str]:
        tool = f"mcp__brain-tools__follow_ups_{name}"
        return {agent for agent, entry in AGENT_REGISTRY.items() if tool in entry["tools"]}

    assert holders("digest") == {"follow_up"}
    assert holders("add") == {"follow_up"}
    assert holders("update") == {"follow_up"}
    assert holders("list") == {"follow_up", "alfred_private"}
    for name in ("digest", "list", "add", "update"):
        tool = f"mcp__brain-tools__follow_ups_{name}"
        assert all(tool not in entry["reply_tools"] for entry in AGENT_REGISTRY.values())
DRAW_CHART = "mcp__brain-tools__draw_chart"
DRAW_IMAGE = "mcp__brain-tools__draw_image"


def test_both_chat_agents_hold_the_draw_tools_and_no_other_shipped_agent_does(monkeypatch, tmp_path) -> None:
    from src.brain.runtime.loader import load_all
    from src.orchestrator.registry import _build_config

    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
    shipped = _build_config(load_all(REPO_ROOT))[0]
    chat_agents = {"alfred", "alfred_private"}
    for name in chat_agents:
        assert {DRAW_CHART, DRAW_IMAGE} <= set(shipped[name]["tools"]), name
    for name in shipped.keys() - chat_agents:
        assert not {DRAW_CHART, DRAW_IMAGE} & set(shipped[name]["reply_tools"]), name
        assert not {DRAW_CHART, DRAW_IMAGE} & set(shipped[name]["tools"]), name


def test_housekeeping_declares_the_media_expiry_as_its_before_step() -> None:
    assert AGENT_REGISTRY["housekeeping"]["before"] == "src.brain.media.expire"
