from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from src.orchestrator import executor


def _entry(**extra):
    return {
        "model": "claude-sonnet-5-5", "tools": [], "reply_tools": [], "allowed_action_types": [],
        "max_turns": 5, "effort": None, "kind": "service", "identity": None, "delivery": None,
        "reply_brief": "", "chat": None, "vault_paths": [], "max_helpers": 0, **extra,
    }


COLLECTOR = "src.brain.followups.collect"


def _drive(monkeypatch, registry, status="success"):
    order = []

    class FakeRunner:
        async def run(self, config, trigger, ctx):
            order.append(("run", ctx["agent_name"]))
            return {"status": status}

    monkeypatch.setattr(executor, "AGENT_REGISTRY", registry)
    monkeypatch.setattr(executor, "_get_prompt", lambda *args, **kwargs: "prompt")
    monkeypatch.setattr(executor, "ClaudeRunner", FakeRunner)
    return order


@pytest.mark.parametrize(
    ("stderr", "tail"),
    [(b"OLDEST\n" + b"y" * 600 + b"\nreading channel D0X", "reading channel D0X"), (None, "(empty)")],
    ids=["with stderr", "without stderr"],
)
def test_a_before_step_that_runs_too_long_returns_minus_one_and_logs_its_stderr(
    monkeypatch, tmp_path, caplog, stderr, tail,
):
    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"], output=b"", stderr=stderr)
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    with caplog.at_level(logging.INFO):
        assert executor.run_before_step("src.probe.step", repo_root=tmp_path) == -1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "src.probe.step" in warnings[0] and "120" in warnings[0]
    assert warnings[0].endswith(tail) and "OLDEST" not in warnings[0]


def test_before_step_returns_the_exit_code_and_forwards_only_the_named_variables(
    monkeypatch, tmp_path,
):
    seen = {}
    def fake_run(args, **kw):
        seen["args"] = args
        seen.update(kw)
        return SimpleNamespace(returncode=2, stdout=b"", stderr=b"")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", "/home/user")
    for name in ("USER_DIR", "BRAIN_VERIFY_RUN", "MEMORY_DB_PATH", "VAULT_DB_PATH", "VAULT_PATH",
                 "BRAIN_SLACK_OFF", "BRAIN_MEDIA"):
        monkeypatch.setenv(name, f"/{name}")
    assert executor.run_before_step("m", repo_root=tmp_path) == 2
    assert seen["args"][1:] == ["-I", "-m", "m"]
    assert seen["cwd"] == tmp_path and seen["timeout"] == 120.0
    assert seen["env"] == {
        "PATH": seen["env"]["PATH"], "HOME": "/home/user", "USER_DIR": "/USER_DIR",
        "BRAIN_VERIFY_RUN": "/BRAIN_VERIFY_RUN", "MEMORY_DB_PATH": "/MEMORY_DB_PATH",
        "VAULT_DB_PATH": "/VAULT_DB_PATH", "VAULT_PATH": "/VAULT_PATH",
        "BRAIN_SLACK_OFF": "/BRAIN_SLACK_OFF",
    }
    assert seen["capture_output"] is True and not seen.get("shell")


def test_a_failing_before_step_logs_the_last_500_characters_of_its_stderr(
    monkeypatch, tmp_path, caplog,
):
    stderr = ("OLDEST-LINE\n" + "x" * 800 + "\nTraceback (most recent call last):\nValueError: boom\n").encode()
    monkeypatch.setattr(
        executor.subprocess, "run",
        lambda args, **kw: SimpleNamespace(returncode=7, stdout=b"", stderr=stderr),
    )
    with caplog.at_level(logging.INFO):
        assert executor.run_before_step("src.probe.step", repo_root=tmp_path) == 7
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "src.probe.step" in warnings[0] and "7" in warnings[0]
    assert warnings[0].endswith(stderr.decode()[-500:])
    assert "OLDEST-LINE" not in warnings[0]


def test_a_failing_before_step_with_undecodable_stderr_still_logs(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        executor.subprocess, "run",
        lambda args, **kw: SimpleNamespace(returncode=3, stdout=b"", stderr=b"bad \xff\xfe bytes"),
    )
    with caplog.at_level(logging.INFO):
        assert executor.run_before_step("m", repo_root=tmp_path) == 3
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "bad" in warnings[0] and "bytes" in warnings[0]


def test_a_clean_before_step_logs_nothing_whatever_its_stderr_says(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        executor.subprocess, "run",
        lambda args, **kw: SimpleNamespace(returncode=0, stdout=b"", stderr=b"warning from a library"),
    )
    with caplog.at_level(logging.INFO):
        assert executor.run_before_step("m", repo_root=tmp_path) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_unset_user_dir_stays_unset_in_the_step(monkeypatch, tmp_path):
    seen = {}
    def fake_run(args, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.delenv("USER_DIR", raising=False)
    executor.run_before_step("m", repo_root=tmp_path)
    assert "USER_DIR" not in seen["env"]


def test_a_failing_before_step_is_logged_as_a_warning_and_the_session_still_runs(
    monkeypatch, caplog,
):
    order = _drive(monkeypatch, {"probe": _entry(before="src.probe.step")})
    monkeypatch.setattr(executor, "run_before_step", lambda module, **kw: 5)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(executor.run_single_agent("probe"))
    assert result == {"status": "success"} and order == [("run", "probe")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "src.probe.step" in warnings[0] and "5" in warnings[0]


def test_a_clean_before_step_logs_nothing(monkeypatch, caplog):
    _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(executor, "run_before_step", lambda module, **kw: 0)
    with caplog.at_level(logging.INFO):
        asyncio.run(executor.run_single_agent("probe"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_before_step_runs_off_the_event_loop_in_the_repo_root(monkeypatch):
    _drive(monkeypatch, {"probe": _entry(before="m")})
    seen = {}
    def fake_step(module, **kw):
        seen["thread"], seen["kw"] = threading.get_ident(), kw
        return 0
    monkeypatch.setattr(executor, "run_before_step", fake_step)
    asyncio.run(executor.run_single_agent("probe"))
    assert seen["thread"] != threading.get_ident()
    assert seen["kw"] == {"repo_root": executor._REPO_ROOT}


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_the_follow_up_run_is_skipped_with_its_collector_when_its_switch_is_off(
    monkeypatch, value,
):
    order = _drive(monkeypatch, {"follow_up": _entry(before="src.brain.followups.collect")})
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    monkeypatch.setenv("BRAIN_FOLLOWUPS", value)
    result = asyncio.run(executor.run_single_agent("follow_up"))
    assert result == {"agent": "follow_up", "status": "skipped"}
    assert order == []


@pytest.mark.parametrize("value", [None, "1", "on"])
def test_the_follow_up_run_collects_then_runs_when_its_switch_is_on_or_unset(monkeypatch, value):
    order = _drive(monkeypatch, {"follow_up": _entry(before="src.brain.followups.collect")})
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    if value is None:
        monkeypatch.delenv("BRAIN_FOLLOWUPS", raising=False)
    else:
        monkeypatch.setenv("BRAIN_FOLLOWUPS", value)
    asyncio.run(executor.run_single_agent("follow_up"))
    assert order == [("before", "src.brain.followups.collect"), ("run", "follow_up")]


def test_the_follow_up_switch_leaves_every_other_agent_alone(monkeypatch):
    order = _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    monkeypatch.setenv("BRAIN_FOLLOWUPS", "0")
    asyncio.run(executor.run_single_agent("probe"))
    assert order == [("before", "m"), ("run", "probe")]


@pytest.fixture
def commits(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
    calls = []
    monkeypatch.setattr(executor, "promote_cursor", lambda user_dir: calls.append(user_dir) or True)
    return calls


def test_a_follow_up_session_that_succeeds_commits_the_collectors_cursor(
    monkeypatch, tmp_path, commits,
):
    order = _drive(monkeypatch, {"follow_up": _entry(before=COLLECTOR)})
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    result = asyncio.run(executor.run_single_agent("follow_up"))
    assert result == {"status": "success"}
    assert order == [("before", COLLECTOR), ("run", "follow_up")]
    assert commits == [tmp_path / "user"]


@pytest.mark.parametrize(
    ("code", "status"),
    [(0, "failed"), (0, "partial"), (1, "success"), (-1, "success")],
    ids=["session failed", "session partial", "collector failed", "collector timed out"],
)
def test_the_cursor_stays_unless_the_collector_and_the_session_both_succeeded(
    monkeypatch, commits, code, status,
):
    _drive(monkeypatch, {"follow_up": _entry(before=COLLECTOR)}, status=status)
    monkeypatch.setattr(executor, "run_before_step", lambda module, **kw: code)
    asyncio.run(executor.run_single_agent("follow_up"))
    assert commits == []


def test_a_session_that_raises_commits_nothing(monkeypatch, commits):
    _drive(monkeypatch, {"follow_up": _entry(before=COLLECTOR)})

    class BrokenRunner:
        async def run(self, config, trigger, ctx):
            raise RuntimeError("Claude exceeded 900s")

    monkeypatch.setattr(executor, "ClaudeRunner", BrokenRunner)
    monkeypatch.setattr(executor, "run_before_step", lambda module, **kw: 0)
    with pytest.raises(RuntimeError):
        asyncio.run(executor.run_single_agent("follow_up"))
    assert commits == []


def test_another_agents_before_step_commits_no_follow_up_cursor(monkeypatch, commits):
    _drive(monkeypatch, {"probe": _entry(before="m")})
    monkeypatch.setattr(executor, "run_before_step", lambda module, **kw: 0)
    asyncio.run(executor.run_single_agent("probe"))
    assert commits == []


def test_while_the_usage_limit_holds_no_before_step_runs(monkeypatch, commits):
    order = _drive(monkeypatch, {"follow_up": _entry(before=COLLECTOR)}, status="failed")
    monkeypatch.setattr(
        executor, "run_before_step", lambda module, **kw: order.append(("before", module)) or 0,
    )
    monkeypatch.setattr(executor.USAGE_LIMIT, "blocked_until", time.time() + 600)
    asyncio.run(executor.run_single_agent("follow_up"))
    assert order == [("run", "follow_up")]
    assert commits == []
