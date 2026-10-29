from __future__ import annotations

import asyncio

import pytest

from src.orchestrator import executor
from src.orchestrator.runners.claude_runner import VERIFY_MARKER, ClaudeRuntimeError


@pytest.fixture(autouse=True)
def _no_api_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "BRAIN_VERIFY_RUN"):
        monkeypatch.delenv(name, raising=False)


def _entry(**extra):
    return {
        "model": "claude-sonnet-5-5", "tools": [], "reply_tools": [], "allowed_action_types": [],
        "max_turns": 5, "effort": None, "kind": "service", "identity": None, "delivery": None,
        "reply_brief": "", "chat": None, "vault_paths": [], "max_helpers": 0, **extra,
    }


def _drive(monkeypatch, registry):
    order = []

    class FakeRunner:
        async def run(self, config, trigger, ctx):
            order.append(("run", ctx["agent_name"]))
            return {"status": "success"}

    monkeypatch.setattr(executor, "AGENT_REGISTRY", registry)
    monkeypatch.setattr(executor, "_get_prompt", lambda *args, **kwargs: "prompt")
    monkeypatch.setattr(executor, "ClaudeRunner", FakeRunner)
    return order


def forbidden(module, **kw):
    raise AssertionError("the before step ran")


def _copies(monkeypatch, tmp_path, *, live=()):
    for name in ("db", "vault", "user"):
        (tmp_path / name).mkdir()
        if name not in live:
            (tmp_path / name / VERIFY_MARKER).write_text("")
    monkeypatch.setenv("BRAIN_VERIFY_RUN", "1")
    monkeypatch.setenv("BRAIN_SLACK_OFF", "1")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "db" / "vault.db"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))


@pytest.mark.parametrize("live, named", [(("user",), "USER_DIR"), (("db",), "MEMORY_DB_PATH")])
def test_a_verification_run_against_live_state_runs_neither_step_nor_session(
    monkeypatch, tmp_path, live, named,
):
    order = _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(executor, "run_before_step", forbidden)
    _copies(monkeypatch, tmp_path, live=live)
    with pytest.raises(ClaudeRuntimeError, match=named):
        asyncio.run(executor.run_single_agent("probe"))
    assert order == []


def test_an_api_key_stops_the_agent_before_its_before_step(monkeypatch):
    order = _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(executor, "run_before_step", forbidden)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(ClaudeRuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(executor.run_single_agent("probe"))
    assert order == []


def test_an_agent_without_a_before_step_is_left_to_its_sessions_own_start_checks(monkeypatch):
    order = _drive(monkeypatch, {"probe": _entry()})
    monkeypatch.setattr(executor, "run_before_step", forbidden)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    asyncio.run(executor.run_single_agent("probe"))
    assert order == [("run", "probe")]


def test_a_verification_run_on_copies_runs_the_before_step_then_the_session(monkeypatch, tmp_path):
    order = _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    _copies(monkeypatch, tmp_path)
    asyncio.run(executor.run_single_agent("probe"))
    assert order == [("before", "m"), ("run", "probe")]
