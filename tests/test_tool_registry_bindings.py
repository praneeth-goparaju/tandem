from __future__ import annotations

import inspect

from src.brain.mcp.server import load_brain_tools


def _registry() -> dict:
    return {s.name: s for s in load_brain_tools()}


def test_occasions_scan_binds_to_the_real_function():
    tools = _registry()
    assert "occasions_scan" in tools, "occasions_scan is not registered at all"
    handler = tools["occasions_scan"].handler
    assert handler.__name__ == "occasions_scan", (
        f"occasions_scan name is bound to the wrong function: {handler.__name__!r} "
        "(a misplaced @brain_tool decorator)"
    )
    params = list(inspect.signature(handler).parameters)
    assert params == ["args"], f"occasions_scan handler must take (args); got {params}"


def test_every_registered_tool_handler_takes_a_single_args_dict():
    _registry()
    bad = []
    for spec in load_brain_tools():
        h = spec.handler
        if not inspect.iscoroutinefunction(h):
            bad.append((spec.name, "not async"))
            continue
        params = [
            p for p in inspect.signature(h).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        if len(params) != 1:
            bad.append((spec.name, f"takes {len(params)} positional params, expected 1"))
    assert not bad, f"tool handlers with wrong shape: {bad}"
