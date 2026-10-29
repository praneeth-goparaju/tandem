import pytest

from src.brain.mcp import tool_registry
from src.brain.mcp.tool_registry import all_tools, brain_tool


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    monkeypatch.setattr(tool_registry, "_REGISTRY", [])


def test_register_one():
    @brain_tool(name="a", description="A", input_schema={"type": "object"})
    async def a(args): return None
    assert len(all_tools()) == 1
    assert all_tools()[0].name == "a"


def test_duplicate_raises():
    @brain_tool(name="a", description="", input_schema={"type": "object"})
    async def a(args): return None
    with pytest.raises(ValueError):
        @brain_tool(name="a", description="", input_schema={"type": "object"})
        async def a2(args): return None


def test_all_tools_returns_copy():
    @brain_tool(name="a", description="", input_schema={"type": "object"})
    async def a(args): return None
    tools = all_tools()
    tools.clear()
    assert len(all_tools()) == 1
