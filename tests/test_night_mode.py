import asyncio
from pathlib import Path

import pytest

from src.brain.runtime.loader import load_all
from src.orchestrator import executor, registry, scheduler
from src.orchestrator.runners import claude_runner

REPO = Path(__file__).resolve().parents[1]
WEB = ("web_search", "web_read")


@pytest.fixture
def shipped(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
    return registry._build_config(load_all(REPO))


def test_night_mode_runs_follow_up_alone_at_23(shipped):
    agents, run_modes, mode_specs = shipped
    night = mode_specs["night"]
    assert night.schedule == "0 23 * * *"
    assert night.sequential == ["follow_up"] and not night.pre and not night.parallel
    assert night.misfire_grace_seconds == 19800
    assert "follow_up" not in mode_specs["all_morning"].agents()
    assert run_modes["all_morning"]["sequential"] == ["alfred_private"]


def test_the_night_job_is_skipped_after_0430(shipped, monkeypatch):
    from src.orchestrator import config

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.setattr(registry, "MODE_SPECS", shipped[2])
    instance = scheduler.create_scheduler()
    assert instance.get_job("night").misfire_grace_time == 19800
    assert instance.get_job("all_morning").misfire_grace_time == 21600


def test_follow_up_has_the_night_budget_and_no_web(shipped):
    follow_up = shipped[0]["follow_up"]
    assert follow_up["max_turns"] == 120
    assert follow_up["timeout_seconds"] == 5400
    tools = follow_up["tools"] + follow_up["reply_tools"]
    assert not [tool for tool in tools if tool.endswith(WEB)]
    assert not [tool for tool in tools if tool.endswith(("mail_search", "mail_get_message", "create_draft"))]
    assert not {"create_calendar_event", "send_draft"} & set(follow_up["allowed_action_types"])


def test_executor_passes_the_agent_timeout(monkeypatch):
    seen: dict = {}

    class FakeRunner:
        async def run(self, config, trigger, context):
            seen.update(context)
            return {"agent": "follow_up", "status": "success"}

    monkeypatch.setattr(executor, "ClaudeRunner", FakeRunner)
    monkeypatch.setattr(executor, "run_before_step", lambda *a, **k: 1)
    monkeypatch.setattr(executor, "_get_prompt", lambda *a, **k: "prompt")
    monkeypatch.setitem(executor.AGENT_REGISTRY, "follow_up", {
        **executor.AGENT_REGISTRY["follow_up"], "timeout_seconds": 5400,
    })
    monkeypatch.setattr(claude_runner, "check_subscription_auth", lambda env: None)
    monkeypatch.setattr(claude_runner, "check_verify_run", lambda env: None)

    asyncio.run(executor.run_single_agent("follow_up"))

    assert seen["timeout"] == 5400


def test_a_reply_does_not_wait_for_a_scheduled_run():
    async def scenario() -> bool:
        async with claude_runner._run_lock("run"):
            reply = claude_runner._run_lock("reply")
            await asyncio.wait_for(reply.acquire(), timeout=0.5)
            reply.release()
            return claude_runner._run_lock("run").locked()

    assert asyncio.run(scenario())
