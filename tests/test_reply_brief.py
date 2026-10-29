from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_agentspec_parses_reply_brief():
    from src.brain.runtime.spec import AgentSpec

    assert AgentSpec(name="x").reply_brief == ""
    assert AgentSpec(name="x", reply_brief="do the thing").reply_brief == "do the thing"


def test_humans_diary_guidance_reaches_the_reply_leg_through_its_area_notes(monkeypatch):
    monkeypatch.setenv("USER_DIR", str(REPO_ROOT / "tests" / "fixtures" / "user-ci"))
    from src.brain.runtime.area_notes import area_notes_text
    from src.brain.runtime.loader import load_all
    from src.orchestrator.registry import AGENT_REGISTRY

    tools = set(AGENT_REGISTRY["humans"]["tools"])
    actions = set(AGENT_REGISTRY["humans"]["allowed_action_types"])
    assert "mcp__brain-tools__propose_action" in tools
    assert {"edit_vault_file", "edit_vault_files"} <= actions
    notes = area_notes_text("humans", load_all(REPO_ROOT))
    for tool in (
        "humans_find", "humans_add_person", "humans_log", "humans_read",
        "humans_resolve_thread", "humans_update_person", "humans_set_occasion",
    ):
        assert tool in notes, f"the humans area notes do not explain {tool}"
    assert "NEVER invent" in notes
