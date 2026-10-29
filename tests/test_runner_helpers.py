from __future__ import annotations

from src.orchestrator.runners.base import (
    resolve_agent_name,
    telemetry_agent_name,
)


def test_telemetry_agent_name_cycle_path():
    assert telemetry_agent_name("router", is_reply=False) == "router"


def test_telemetry_agent_name_reply_path():
    assert telemetry_agent_name("router", is_reply=True) == "router_reply"


def test_telemetry_agent_name_empty_uses_unknown_default():
    assert telemetry_agent_name("", is_reply=False) == "unknown"
    assert telemetry_agent_name("", is_reply=True) == "unknown_reply"


def test_resolve_agent_name_prefers_context():
    out = resolve_agent_name({"agent_name": "ctx"}, {"name": "cfg"})
    assert out == "ctx"


def test_resolve_agent_name_falls_back_to_cfg():
    out = resolve_agent_name({}, {"name": "cfg"})
    assert out == "cfg"


def test_resolve_agent_name_falls_back_to_unknown():
    out = resolve_agent_name({}, {})
    assert out == "unknown"


def test_resolve_agent_name_empty_strings_treated_as_missing():
    out = resolve_agent_name({"agent_name": ""}, {"name": "cfg"})
    assert out == "cfg"
