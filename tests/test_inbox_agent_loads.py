from pathlib import Path

from src.brain.runtime.loader import load_agents

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_inbox_agent_loads():
    agents = load_agents(REPO_ROOT / "config")
    assert "inbox" in agents
    spec = agents["inbox"]
    assert spec.kind == "service"
    assert spec.delivery is not None and spec.delivery.slack is not None
    assert "mcp__brain-tools__mail_search" in spec.mcp_tools
    body = spec.body
    assert '_area_owner.md' not in body
    assert "external_content" in body
