from __future__ import annotations

import asyncio


def _registry_names():
    from src.brain.mcp.server import load_brain_tools

    return {spec.name for spec in load_brain_tools()}


def test_write_tools_absent_from_registry():
    names = _registry_names()
    assert "store_knowledge" not in names
    assert "memory_store_knowledge" not in names


def test_store_finding_cannot_auto_promote_knowledge(isolated_db):
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.findings import store_finding

    with caller_scope(ToolCaller(agent="discovery", area="discovery")):
        asyncio.run(store_finding({
            "category": "IMPORTANT",
            "title": "Evidence stays in the finding ledger",
            "detail": "Durable promotion requires human confirmation.",
            "reasoning": "Regression coverage for the frozen archive.",
            "tags": "legacy-tag",
            "recurrence": "annual",
        }))

    count = isolated_db.get_db().execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    assert count == 0


def test_read_tool_still_registered():
    assert "search_knowledge" in _registry_names()
