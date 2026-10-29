from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from src.brain.net_security import public_query_terms
from src.orchestrator.runners import claude_runner as runner_mod
from src.orchestrator.runners.base import ClaudeRuntimeError
from src.orchestrator.runners.claude_runner import (
    CLAUDE_MODELS,
    DENIED_TOOLS,
    VERIFY_MARKER,
    ClaudeRunner,
    build_options,
    check_session_tools,
    check_subscription_auth,
    check_verify_run,
    denied_for,
    feature_on,
    model_for_agent,
    split_tools,
)


@pytest.fixture(autouse=True)
def _no_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "BRAIN_VERIFY_RUN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _feature_switches_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BRAIN_DIRECT_EDITS", "BRAIN_HELPERS"):
        monkeypatch.setenv(name, "1")


def test_split_tools_maps_read_capabilities_and_brain_names() -> None:
    builtins, brain = split_tools({
        "tools": [
            "Read", "Glob", "Grep",
            "mcp__brain-tools__store_finding",
            "mcp__brain-tools__tasks_create",
            "mcp__brain-tools__action_note_complete",
        ],
    })
    assert builtins == ["Read", "Glob", "Grep"]
    assert brain == ["store_finding", "tasks_create", "action_note_complete"]


def test_split_tools_accepts_edit_write_agent() -> None:
    builtins, brain = split_tools({"tools": ["Read", "Edit", "Write", "Agent"]})
    assert builtins == ["Read", "Edit", "Write", "Agent"]
    assert brain == []
    with pytest.raises(ClaudeRuntimeError, match="unsupported tool"):
        split_tools({"tools": ["Read", "Bash"]})


@pytest.mark.parametrize("tool", ["Bash", "MultiEdit", "Task", "WebSearch", "WebFetch"])
def test_split_tools_rejects_unsupported_capabilities(tool: str) -> None:
    with pytest.raises(ClaudeRuntimeError, match="unsupported tool"):
        split_tools({"tools": [tool]})


def test_denied_for_removes_granted_builtins_only() -> None:
    denied = denied_for(["Read", "Edit"])
    assert "Edit" not in denied
    for tool in ("Write", "Agent", "Bash"):
        assert tool in denied
    assert set(DENIED_TOOLS) - set(denied) == {"Edit"}
    assert denied_for([]) == list(DENIED_TOOLS)
    assert denied_for(["Read", "Glob", "Grep"]) == list(DENIED_TOOLS)


def test_denied_for_drops_task_together_with_agent() -> None:
    granted = denied_for(["Read", "Agent"])
    assert "Agent" not in granted
    assert "Task" not in granted
    assert set(DENIED_TOOLS) - set(granted) == {"Agent", "Task"}


@pytest.mark.parametrize(("value", "expected"), [
    (None, True),
    ("1", True),
    ("true", True),
    ("", True),
    ("maybe", True),
    ("0", False),
    ("false", False),
    ("No", False),
    ("off", False),
    ("  OFF ", False),
])
def test_feature_on_reads_the_switch(value: str | None, expected: bool) -> None:
    environ = {} if value is None else {"BRAIN_HELPERS": value}
    assert feature_on("BRAIN_HELPERS", environ) is expected


def test_model_for_agent_accepts_only_claude_ids() -> None:
    assert CLAUDE_MODELS == ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
    for model in CLAUDE_MODELS:
        assert model_for_agent({"model": model}) == model
    assert model_for_agent({}) == "claude-sonnet-5-5"
    with pytest.raises(ClaudeRuntimeError, match="gpt-5.6-terra"):
        model_for_agent({"model": "gpt-5.6-terra"})


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_subscription_auth_refuses_api_key(name: str) -> None:
    with pytest.raises(ClaudeRuntimeError, match=name):
        check_subscription_auth({name: "sk-test"})
    check_subscription_auth({name: ""})
    check_subscription_auth({})


def _verify_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    db_dir = tmp_path / "db"
    vault = tmp_path / "vault"
    overlay = tmp_path / "user"
    for folder in (db_dir, vault, overlay):
        folder.mkdir(exist_ok=True)
        (folder / VERIFY_MARKER).write_text("")
    env = {
        "BRAIN_VERIFY_RUN": "1",
        "BRAIN_SLACK_OFF": "1",
        "MEMORY_DB_PATH": str(db_dir / "vault.db"),
        "VAULT_PATH": str(vault),
        "USER_DIR": str(overlay),
    }
    env.update(overrides)
    return env


def test_verify_run_off_returns_false() -> None:
    assert check_verify_run({}) is False
    assert check_verify_run({"BRAIN_VERIFY_RUN": "0"}) is False


def test_verify_run_on_and_safe_returns_true(tmp_path: Path) -> None:
    assert check_verify_run(_verify_env(tmp_path)) is True


def test_verify_run_requires_slack_off(tmp_path: Path) -> None:
    with pytest.raises(ClaudeRuntimeError, match="BRAIN_SLACK_OFF"):
        check_verify_run(_verify_env(tmp_path, BRAIN_SLACK_OFF=""))


def test_verify_run_requires_marker_beside_db_and_in_vault(tmp_path: Path) -> None:
    env = _verify_env(tmp_path)
    (tmp_path / "db" / VERIFY_MARKER).unlink()
    with pytest.raises(ClaudeRuntimeError, match="MEMORY_DB_PATH"):
        check_verify_run(env)
    env = _verify_env(tmp_path)
    (tmp_path / "vault" / VERIFY_MARKER).unlink()
    with pytest.raises(ClaudeRuntimeError, match="VAULT_PATH"):
        check_verify_run(env)
    with pytest.raises(ClaudeRuntimeError, match="VAULT_PATH"):
        check_verify_run({k: v for k, v in _verify_env(tmp_path).items() if k != "VAULT_PATH"})


def test_a_verification_run_needs_a_copied_user_dir(tmp_path: Path) -> None:
    for d in ("db", "vault"):
        (tmp_path / d).mkdir()
        (tmp_path / d / ".brain-verify-copy").touch()
    env = {"BRAIN_VERIFY_RUN": "1", "BRAIN_SLACK_OFF": "1",
           "MEMORY_DB_PATH": str(tmp_path / "db" / "memory.db"), "VAULT_PATH": str(tmp_path / "vault")}
    with pytest.raises(ClaudeRuntimeError, match="USER_DIR"):
        check_verify_run(env)
    (tmp_path / "live-user").mkdir()
    with pytest.raises(ClaudeRuntimeError, match="USER_DIR"):
        check_verify_run({**env, "USER_DIR": str(tmp_path / "live-user")})
    with pytest.raises(ClaudeRuntimeError, match="USER_DIR"):
        check_verify_run({**env, "USER_DIR": "  "})
    (tmp_path / "live-user" / VERIFY_MARKER).touch()
    assert check_verify_run({**env, "USER_DIR": str(tmp_path / "live-user")}) is True


def _options(tmp_path: Path, brain: list[str], server: object | None):
    return build_options(
        system_prompt="You are a test agent.",
        model="claude-haiku-5-5",
        effort="low",
        max_turns=7,
        builtins=["Read", "Grep"],
        brain_tool_names=brain,
        brain_server=server,  # type: ignore[arg-type]
        hooks={},
        cwd=tmp_path,
        denied=denied_for(["Read", "Grep"]),
        agents=None,
        env={},
    )


def test_options_isolate_session(tmp_path: Path) -> None:
    server = {"type": "sdk", "name": "brain-tools", "instance": object()}
    options = _options(tmp_path, ["mail_search", "store_finding"], server)
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.tools == ["Read", "Grep"]
    assert options.allowed_tools == [
        "Read", "Grep", "mcp__brain-tools__mail_search", "mcp__brain-tools__store_finding",
    ]
    assert set(DENIED_TOOLS) <= set(options.disallowed_tools)
    assert options.extra_args == {"no-session-persistence": None}
    assert options.skills == []
    assert options.env == {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    }
    assert options.mcp_servers == {"brain-tools": server}
    assert options.model == "claude-haiku-5-5"
    assert options.effort == "low"
    assert options.max_turns == 7
    assert options.system_prompt == "You are a test agent."
    assert Path(options.cwd) == tmp_path


def test_options_without_brain_tools_have_no_server(tmp_path: Path) -> None:
    options = _options(tmp_path, [], None)
    assert options.mcp_servers == {}
    assert options.allowed_tools == ["Read", "Grep"]


_ALLOWED = {"Read", "mcp__brain-tools__mail_search"}


def test_session_tools_accepts_exact_grant() -> None:
    init = {"tools": ["Read", "mcp__brain-tools__mail_search"],
            "mcp_servers": [{"name": "brain-tools", "status": "connected"}]}
    assert check_session_tools(init, _ALLOWED, set(DENIED_TOOLS)) is None


def test_session_tools_allows_denied_extras() -> None:
    init = {"tools": ["Read", "Bash", "WebFetch"], "mcp_servers": []}
    assert check_session_tools(init, _ALLOWED, set(DENIED_TOOLS)) is None


def test_session_tools_rejects_unknown_extra() -> None:
    init = {"tools": ["Read", "SomethingNew"], "mcp_servers": []}
    reason = check_session_tools(init, _ALLOWED, set(DENIED_TOOLS))
    assert reason and "SomethingNew" in reason


@pytest.mark.parametrize("tool", ["Edit", "Write", "Agent", "Task"])
def test_session_offering_edit_write_or_agent_fails_without_grant(tool: str) -> None:
    denied = set(denied_for(["Read"]))
    assert tool in denied
    offered = {"tools": ["Read", tool]}
    reason = check_session_tools(offered, {"Read"}, denied)
    assert reason and tool in reason
    assert check_session_tools(offered, {"Read", tool}, denied) is None
    assert check_session_tools({"tools": ["Read", "Bash"]}, {"Read"}, denied) is None


@pytest.mark.parametrize("offered", ["Task", "Agent"])
def test_task_and_agent_are_one_tool_to_the_start_up_check(offered: str) -> None:
    granted, denied = {"Read", "Agent"}, set(denied_for(["Read", "Agent"]))
    assert check_session_tools({"tools": ["Read", offered]}, granted, denied) is None
    assert check_session_tools({"tools": ["Read", "Task", "Agent"]}, granted, denied) is None
    reason = check_session_tools({"tools": ["Read", offered]}, {"Read"}, set(denied_for(["Read"])))
    assert reason and offered in reason


def test_session_with_edit_granted_still_fails_on_an_ungranted_write() -> None:
    allowed = {"Read", "Edit"}
    denied = set(denied_for(["Read", "Edit"]))
    reason = check_session_tools({"tools": ["Read", "Edit", "Write"]}, allowed, denied)
    assert reason and "Write" in reason
    assert "Edit" not in reason


def test_session_tools_rejects_extra_mcp_server() -> None:
    init = {"tools": ["Read"], "mcp_servers": [
        {"name": "brain-tools", "status": "connected"},
        {"name": "gmail", "status": "connected"},
    ]}
    reason = check_session_tools(init, _ALLOWED, set(DENIED_TOOLS))
    assert reason and "gmail" in reason


_USAGE = {
    "input_tokens": 120, "output_tokens": 30,
    "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50,
}


def _init(tools: list[str] | None = None, servers: list[dict] | None = None) -> SystemMessage:
    return SystemMessage(
        subtype="init", data={"tools": tools or ["Read"], "mcp_servers": servers or []},
    )


def _text(text: str, *, tools: tuple[str, ...] = ()) -> AssistantMessage:
    blocks: list[Any] = [ToolUseBlock(id=f"t{i}", name=n, input={}) for i, n in enumerate(tools)]
    blocks.append(TextBlock(text=text))
    return AssistantMessage(content=blocks, model="claude-sonnet-5-5")


def _result(text: str | None = "done", *, subtype: str = "success", is_error: bool = False,
            stop_reason: str = "end_turn") -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=10, duration_api_ms=8, is_error=is_error,
        num_turns=3, session_id="s", stop_reason=stop_reason, total_cost_usd=0.0123,
        usage=dict(_USAGE), result=text,
    )


class FakeQuery:
    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.yielded = 0

    def __call__(self, *, prompt: Any, options: Any):
        self.calls.append({"prompt": prompt, "options": options})

        async def stream():
            for item in self.script:
                if item == "sleep":
                    await asyncio.sleep(30)
                self.yielded += 1
                yield item

        return stream()


_AGENT = {"name": "inbox", "model": "claude-sonnet-5-5", "tools": ["Read"], "max_turns": 5}


def _ctx(**extra: Any) -> dict[str, Any]:
    return {"system_prompt": "You are the inbox agent.", "agent_name": "inbox", **extra}


def _rows(db) -> list[dict[str, Any]]:
    conn = db.get_db()
    cur = conn.execute(
        "SELECT agent, provider, model, status, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, cost_usd, error FROM agent_runs",
    )
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(runner_mod, "VAULT_PATH", root)
    return root


def test_run_success_maps_result_fields(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _text("hi", tools=("Read", "Read")), _result("all quiet"))
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "Run your checks.", _ctx()))
    assert result["status"] == "success"
    assert result["response"] == "all quiet"
    assert result["provider"] == "claude"
    assert result["model"] == "claude-sonnet-5-5"
    assert (result["input_tokens"], result["output_tokens"]) == (120, 30)
    assert (result["cache_read_tokens"], result["cache_creation_tokens"]) == (900, 50)
    assert result["turns"] == 3
    assert result["tools"] == 2
    assert result["cost_usd"] == pytest.approx(0.0123)
    [row] = _rows(isolated_db)
    assert row["agent"] == "inbox"
    assert row["provider"] == "claude"
    assert row["model"] == "claude-sonnet-5-5"
    assert row["status"] == "success"
    assert row["input_tokens"] == 120


def test_system_prompt_names_the_model(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _result())
    asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    options = fake.calls[0]["options"]
    assert options.system_prompt.endswith("You are running as `claude-sonnet-5-5`.")
    assert options.system_prompt.startswith("You are the inbox agent.")
    assert Path(options.cwd) == vault


def test_caller_context_matches_pi_rules(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "_owner_place_terms", lambda: frozenset({"northtown"}))
    captured: dict[str, Any] = {}

    async def specs():
        return [{"name": "store_finding", "description": "s", "input_schema": {}}]

    def fake_server(specs_, allowed, caller_context, **kw):
        captured["caller"] = dict(caller_context)
        captured["allowed"] = allowed
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    monkeypatch.setattr(runner_mod, "load_brain_tool_specs", specs)
    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    agent = {**_AGENT, "tools": ["mcp__brain-tools__store_finding"],
             "allowed_action_types": ["mail_archive", "mail_archive"]}
    fake = FakeQuery(_init(["mcp__brain-tools__store_finding"],
                           [{"name": "brain-tools", "status": "connected"}]), _result())
    text = "what is the weather in Springfield tomorrow"
    asyncio.run(ClaudeRunner(query_fn=fake).run_reply(
        text, agent, _ctx(area="inbox", msg={"quoted_text": "earlier message"}),
    ))
    caller = captured["caller"]
    assert caller["agent"] == "inbox"
    assert caller["area"] == "inbox"
    assert caller["allowed_action_types"] == ["mail_archive"]
    assert caller["direct_action_types"] == []
    assert caller["is_reply"] is True
    assert caller["untrusted_input"] is True
    assert caller["web_query_terms"] == sorted(public_query_terms(text[:2000]) | {"northtown"})
    assert captured["allowed"] == ["store_finding"]


def test_prompt_is_one_user_turn_with_images(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _text("seen"), _result("seen"))
    image = {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    asyncio.run(ClaudeRunner(query_fn=fake).run_reply(
        "what is this?", dict(_AGENT), _ctx(msg={"images": [image]}),
    ))

    async def drain(stream):
        return [item async for item in stream]

    turns = asyncio.run(drain(fake.calls[0]["prompt"]))
    assert len(turns) == 1
    turn = turns[0]
    assert turn["type"] == "user"
    assert turn["message"]["role"] == "user"
    assert turn["message"]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "text", "text": "what is this?"},
    ]


def test_scheduled_run_may_end_quietly(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _result(None))
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert result["status"] == "success"
    assert result["response"] == ""


def test_reply_without_text_or_delivery_fails(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _result(None))
    result = asyncio.run(ClaudeRunner(query_fn=fake).run_reply("hi", dict(_AGENT), _ctx()))
    assert result["status"] == "failed"
    assert "without a text response" in (result["error"] or "")


def test_turn_limit_is_partial(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _text("half"), _result(None, subtype="error_max_turns", is_error=True))
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert result["status"] == "partial"
    assert result["turn_limit_reached"] is True
    assert result["response"] == "half"


def test_result_error_is_failed(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), _result("boom", subtype="error_during_execution", is_error=True))
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert result["status"] == "failed"
    assert result["error"]
    assert _rows(isolated_db)[0]["status"] == "failed"


def test_unexpected_session_tool_fails_before_first_turn(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(["Read", "SomethingNew"]), _text("should not be read"), _result())
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert result["status"] == "failed"
    assert "SomethingNew" in (result["error"] or "")
    assert fake.yielded == 1


def test_ungranted_write_offered_by_the_session_fails_before_first_turn(
    isolated_db, vault: Path,
) -> None:
    fake = FakeQuery(_init(["Read", "Write"]), _text("should not be read"), _result())
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert result["status"] == "failed"
    assert "Write" in (result["error"] or "")
    assert fake.yielded == 1


def test_a_run_granted_agent_starts_when_the_session_offers_task(
    isolated_db, vault: Path,
) -> None:
    agent = {**_AGENT, "tools": ["Read", "Agent"], "max_helpers": 3}
    fake = FakeQuery(_init(["Read", "Task"]), _result())

    result = asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx()))

    assert result["status"] == "success"
    assert fake.calls[0]["options"].hooks["PreToolUse"][0].matcher is None


def test_a_session_offering_task_without_the_grant_stops_before_the_first_turn(
    isolated_db, vault: Path,
) -> None:
    fake = FakeQuery(_init(["Read", "Task"]), _text("should not be read"), _result())

    result = asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))

    assert result["status"] == "failed"
    assert "Task" in (result["error"] or "")
    assert fake.yielded == 1


def test_run_options_carry_the_runs_denied_list_and_scrubbed_env(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "abc")
    agent = {**_AGENT, "tools": ["Read", "Edit"]}
    fake = FakeQuery(_init(["Read", "Edit"]), _result())
    result = asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx()))
    options = fake.calls[0]["options"]
    assert result["status"] == "success"
    assert options.tools == ["Read", "Edit"]
    assert options.allowed_tools == ["Read", "Edit"]
    assert options.disallowed_tools == [
        "Write", "MultiEdit", "NotebookEdit", "Bash", "Agent", "Task",
        "WebSearch", "WebFetch", "Skill",
    ]
    assert "Edit" not in options.disallowed_tools
    assert options.agents is None
    assert options.env["SLACK_BOT_TOKEN"] == ""
    assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_timeout_records_timeout_and_raises(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), "sleep", _result())
    with pytest.raises(ClaudeRuntimeError, match="exceeded"):
        asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx(timeout=0.2)))
    assert _rows(isolated_db)[0]["status"] == "timeout"


def test_cancel_records_cancelled_and_reraises(isolated_db, vault: Path) -> None:
    fake = FakeQuery(_init(), "sleep", _result())

    async def scenario():
        task = asyncio.create_task(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
        await asyncio.sleep(0.1)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert _rows(isolated_db)[0]["status"] == "cancelled"


def test_auth_refusal_records_failed_run(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = FakeQuery(_init(), _result())
    with pytest.raises(ClaudeRuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(ClaudeRunner(query_fn=fake).run(dict(_AGENT), "go", _ctx()))
    assert fake.calls == []
    [row] = _rows(isolated_db)
    assert row["status"] == "failed"
    assert "ANTHROPIC_API_KEY" in row["error"]


def _copied_overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overlay = tmp_path / "user-copy"
    overlay.mkdir()
    (overlay / VERIFY_MARKER).write_text("")
    monkeypatch.setenv("USER_DIR", str(overlay))


def _hook(fake: FakeQuery):
    matcher = fake.calls[0]["options"].hooks["PreToolUse"][0]
    return matcher.hooks[0]


def _decision(output: dict[str, Any]) -> str | None:
    return (output.get("hookSpecificOutput") or {}).get("permissionDecision")


async def _ask(hook, tool_name: str, tool_input: dict[str, Any], *,
               tool_use_id: str = "t", **extra: Any) -> dict[str, Any]:
    return await hook({"hook_event_name": "PreToolUse", "tool_name": tool_name,
                       "tool_input": tool_input, "tool_use_id": tool_use_id, **extra},
                      tool_use_id, {"signal": None})


def _brain_agent(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> dict[str, Any]:
    async def specs():
        return [{"name": n, "description": n, "input_schema": {}} for n in names]

    monkeypatch.setattr(runner_mod, "load_brain_tool_specs", specs)
    return {**_AGENT, "tools": ["Read", *(f"mcp__brain-tools__{n}" for n in names)]}


def test_hook_denies_out_of_root_read_and_tainted_mutation(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _brain_agent(monkeypatch, ["mail_search", "create_draft"])
    fake = FakeQuery(
        _init(["Read", "mcp__brain-tools__mail_search", "mcp__brain-tools__create_draft"],
              [{"name": "brain-tools", "status": "connected"}]),
        _result(),
    )
    asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx(areas=["family"])))
    hook = _hook(fake)

    async def scenario():
        denied = await _ask(hook, "Read", {"file_path": "/etc/passwd"})
        inside = await _ask(hook, "Read", {"file_path": str(vault / "notes.md")})
        read_mail = await _ask(hook, "mcp__brain-tools__mail_search", {"query": "x"})
        draft = await _ask(hook, "mcp__brain-tools__create_draft", {})
        return denied, inside, read_mail, draft

    denied, inside, read_mail, draft = asyncio.run(scenario())
    assert _decision(denied) == "deny"
    assert _decision(inside) is None
    assert _decision(read_mail) is None
    assert _decision(draft) == "deny"
    assert "create_draft" in draft["hookSpecificOutput"]["permissionDecisionReason"]


def test_verify_run_denies_direct_mutators(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_dir = tmp_path / "copy"
    db_dir.mkdir()
    (db_dir / VERIFY_MARKER).write_text("")
    (vault / VERIFY_MARKER).write_text("")
    _copied_overlay(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_VERIFY_RUN", "1")
    monkeypatch.setenv("BRAIN_SLACK_OFF", "1")
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_dir / "vault.db"))
    monkeypatch.setenv("VAULT_PATH", str(vault))
    agent = _brain_agent(monkeypatch, ["create_draft"])
    fake = FakeQuery(_init(["Read", "mcp__brain-tools__create_draft"],
                           [{"name": "brain-tools", "status": "connected"}]), _result())
    asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx()))
    draft = asyncio.run(_ask(_hook(fake), "mcp__brain-tools__create_draft", {}))
    assert _decision(draft) == "deny"
    assert "verification run" in draft["hookSpecificOutput"]["permissionDecisionReason"]
    reminder = asyncio.run(_ask(_hook(fake), "mcp__brain-tools__create_reminder", {}))
    assert _decision(reminder) == "deny"


def test_verify_run_forces_confirm_through_server(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db_dir = tmp_path / "copy"
    db_dir.mkdir()
    (db_dir / VERIFY_MARKER).write_text("")
    (vault / VERIFY_MARKER).write_text("")
    _copied_overlay(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_VERIFY_RUN", "1")
    monkeypatch.setenv("BRAIN_SLACK_OFF", "1")
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_dir / "vault.db"))
    monkeypatch.setenv("VAULT_PATH", str(vault))
    captured: dict[str, Any] = {}

    def fake_server(specs_, allowed, caller_context, **kw):
        captured.update(kw)
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    agent = _brain_agent(monkeypatch, ["propose_action"])
    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    fake = FakeQuery(_init(["Read", "mcp__brain-tools__propose_action"],
                           [{"name": "brain-tools", "status": "connected"}]), _result())
    asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx()))
    assert captured["verify"] is True


def _post_hook(fake: FakeQuery):
    matcher = fake.calls[0]["options"].hooks["PostToolUse"][0]
    assert matcher.matcher == "Edit|Write"
    return matcher.hooks[0]


async def _after(hook, tool_name: str, tool_input: dict[str, Any], *,
                 tool_use_id: str = "t") -> dict[str, Any]:
    return await hook({"hook_event_name": "PostToolUse", "tool_name": tool_name,
                       "tool_input": tool_input, "tool_response": {}, "tool_use_id": tool_use_id},
                      tool_use_id, {"signal": None})


def _sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _editor(monkeypatch: pytest.MonkeyPatch, *, brain: tuple[str, ...] = ("mail_search",),
            **extra: Any) -> dict[str, Any]:
    agent = _brain_agent(monkeypatch, list(brain))
    return {
        **agent,
        "tools": ["Read", "Edit", "Write", "Agent", *agent["tools"][1:]],
        "vault_paths": ["Health/"],
        "max_helpers": 2,
        **extra,
    }


def _start(agent: dict[str, Any], *, reply: bool = False, **context: Any) -> FakeQuery:
    fake = FakeQuery(_init(), _result())
    runner = ClaudeRunner(query_fn=fake)
    if reply:
        run = runner.run_reply("go", agent, _ctx(**context))
    else:
        run = runner.run(agent, "go", _ctx(**context))
    assert asyncio.run(run)["status"] == "success"
    return fake


_NOTE = "weight: 83 kg\nplan: walk daily\n" * 4


@pytest.fixture(autouse=True)
def _edit_backups_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "user").mkdir(exist_ok=True)
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
_HELPER_CALL = {"description": "look it up", "prompt": "find it", "subagent_type": "helper"}


def _tracker(vault: Path) -> Path:
    folder = vault / "Health"
    folder.mkdir()
    note = folder / "tracker.md"
    note.write_text(_NOTE)
    return note


def _change(note: Path, old: str = "83 kg", new: str = "82 kg") -> dict[str, Any]:
    return {"file_path": str(note), "old_string": old, "new_string": new}


def _events(db) -> list[dict[str, Any]]:
    cur = db.get_db().execute(
        "SELECT actor, action, subject, trust, payload_hash, detail FROM events ORDER BY id",
    )
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _edits(db) -> list[dict[str, Any]]:
    cur = db.get_db().execute(
        "SELECT agent, run_label, tool, path, sha_before, sha_after FROM vault_edits ORDER BY id",
    )
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def test_unattended_run_denies_edit_after_mail(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    fake = _start(_editor(monkeypatch))
    hook = _hook(fake)

    async def scenario():
        before_mail = await _ask(hook, "Edit", _change(note), tool_use_id="e1")
        await _ask(hook, "mcp__brain-tools__mail_search", {"query": "x"}, tool_use_id="m1")
        after_mail = await _ask(hook, "Edit", _change(note), tool_use_id="e2")
        return before_mail, after_mail

    before_mail, after_mail = asyncio.run(scenario())
    assert before_mail == {}
    assert _decision(after_mail) == "deny"
    assert "propose_action" in after_mail["hookSpecificOutput"]["permissionDecisionReason"]


def test_chat_run_allows_edit_after_mail_and_records_it(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    fake = _start(_editor(monkeypatch), reply=True)
    pre, post = _hook(fake), _post_hook(fake)

    async def scenario():
        await _ask(pre, "mcp__brain-tools__mail_search", {"query": "x"}, tool_use_id="m1")
        allowed = await _ask(pre, "Edit", _change(note), tool_use_id="e1")
        note.write_text(_NOTE.replace("83 kg", "82 kg"))
        recorded = await _after(post, "Edit", _change(note), tool_use_id="e1")
        return allowed, recorded

    allowed, recorded = asyncio.run(scenario())
    assert allowed == {}
    assert recorded == {}
    [row] = _edits(isolated_db)
    assert row["agent"] == "inbox"
    assert row["run_label"].startswith("inbox_reply@")
    assert row["tool"] == "Edit"
    assert row["path"] == "Health/tracker.md"
    assert row["sha_before"] == _sha256(_NOTE)
    assert row["sha_after"] == _sha256(_NOTE.replace("83 kg", "82 kg"))


def test_edit_with_a_relative_path_is_recorded_against_the_vault(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    relative = {"file_path": "Health/tracker.md", "old_string": "83 kg", "new_string": "82 kg"}
    fake = _start(_editor(monkeypatch))
    pre, post = _hook(fake), _post_hook(fake)

    async def scenario():
        assert await _ask(pre, "Edit", relative) == {}
        note.write_text(_NOTE.replace("83 kg", "82 kg"))
        await _after(post, "Edit", relative)

    asyncio.run(scenario())
    [row] = _edits(isolated_db)
    assert row["path"] == "Health/tracker.md"
    assert row["sha_before"] == _sha256(_NOTE)
    assert row["sha_after"] == _sha256(_NOTE.replace("83 kg", "82 kg"))


def test_a_padded_edit_path_is_recorded_trimmed_with_its_hashes(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    padded = {"file_path": " Health/tracker.md\n", "old_string": "83 kg", "new_string": "82 kg"}
    fake = _start(_editor(monkeypatch))
    pre, post = _hook(fake), _post_hook(fake)

    async def scenario():
        assert await _ask(pre, "Edit", padded) == {}
        note.write_text(_NOTE.replace("83 kg", "82 kg"))
        await _after(post, "Edit", padded)

    asyncio.run(scenario())
    [row] = _edits(isolated_db)
    assert row["path"] == "Health/tracker.md"
    assert row["sha_before"] == _sha256(_NOTE)
    assert row["sha_after"] == _sha256(_NOTE.replace("83 kg", "82 kg"))


def test_hash_before_is_kept_for_each_tool_call(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _tracker(vault)
    second = vault / "Health" / "sleep.md"
    second.write_text("sleep: 7 h\n")
    fake = _start(_editor(monkeypatch))
    pre, post = _hook(fake), _post_hook(fake)
    change_second = {"file_path": str(second), "old_string": "7 h", "new_string": "8 h"}

    async def scenario():
        await _ask(pre, "Edit", _change(first), tool_use_id="e1")
        await _ask(pre, "Edit", change_second, tool_use_id="e2")
        first.write_text(_NOTE.replace("83 kg", "82 kg"))
        second.write_text("sleep: 8 h\n")
        await _after(post, "Edit", change_second, tool_use_id="e2")
        await _after(post, "Edit", _change(first), tool_use_id="e1")

    asyncio.run(scenario())
    rows = {row["path"]: row for row in _edits(isolated_db)}
    assert rows["Health/tracker.md"]["sha_before"] == _sha256(_NOTE)
    assert rows["Health/sleep.md"]["sha_before"] == _sha256("sleep: 7 h\n")
    assert rows["Health/sleep.md"]["sha_after"] == _sha256("sleep: 8 h\n")


def test_write_is_recorded_with_no_hash_before(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tracker(vault)
    created = vault / "Health" / "new.md"
    call = {"file_path": str(created), "content": "a new note\n"}
    fake = _start(_editor(monkeypatch))
    pre, post = _hook(fake), _post_hook(fake)

    async def scenario():
        assert await _ask(pre, "Write", call) == {}
        created.write_text("a new note\n")
        await _after(post, "Write", call)

    asyncio.run(scenario())
    [row] = _edits(isolated_db)
    assert (row["tool"], row["path"]) == ("Write", "Health/new.md")
    assert row["sha_before"] is None
    assert row["sha_after"] == _sha256("a new note\n")


def test_only_edit_and_write_reach_the_ledger(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    post = _post_hook(_start(_editor(monkeypatch, brain=())))

    async def scenario():
        await _after(post, "Read", {"file_path": str(note)}, tool_use_id="r1")
        await _after(post, "mcp__brain-tools__mail_search", {"query": "x"}, tool_use_id="m1")

    asyncio.run(scenario())
    assert _edits(isolated_db) == []
    assert _events(isolated_db) == []


def test_helper_cap_and_type_are_enforced_in_hook(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _start(_editor(monkeypatch, brain=(), max_helpers=2))
    hook = _hook(fake)

    async def scenario():
        other = await _ask(hook, "Agent", {**_HELPER_CALL, "subagent_type": "general-purpose"})
        started = [await _ask(hook, "Agent", _HELPER_CALL, tool_use_id=f"a{i}") for i in range(3)]
        return started, other

    started, other = asyncio.run(scenario())
    assert started[:2] == [{}, {}]
    assert _decision(started[2]) == "deny"
    assert "2" in started[2]["hookSpecificOutput"]["permissionDecisionReason"]
    assert _decision(other) == "deny"
    assert "helper" in other["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_task_call_counts_as_a_helper_start(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _hook(_start(_editor(monkeypatch, brain=(), max_helpers=1)))

    async def scenario():
        return (
            await _ask(hook, "Task", _HELPER_CALL, tool_use_id="a1"),
            await _ask(hook, "Agent", _HELPER_CALL, tool_use_id="a2"),
        )

    first, second = asyncio.run(scenario())
    assert first == {}
    assert _decision(second) == "deny"
    assert [event["action"] for event in _events(isolated_db)] == ["helper_start", "tool_refused"]


def test_helper_tool_calls_pass_through_the_same_rules(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_server(specs_, allowed, caller_context, **kw):
        captured["caller"] = caller_context
        captured["state"] = kw["state"]
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    fake = _start(_editor(monkeypatch, brain=("mail_search", "propose_action")))
    hook = _hook(fake)
    state, caller = captured["state"], captured["caller"]
    assert state.seen is False

    async def scenario():
        parent = await _ask(hook, "mcp__brain-tools__propose_action", {"type": "x"})
        denied = await _ask(hook, "mcp__brain-tools__propose_action", {"type": "x"},
                            tool_use_id="h1-a", agent_id="h1")
        allowed = await _ask(hook, "mcp__brain-tools__mail_search", {"query": "x"},
                             tool_use_id="h1-b", agent_id="h1")
        return parent, denied, allowed

    parent, denied, allowed = asyncio.run(scenario())
    assert parent == {}
    assert _decision(denied) == "deny"
    assert "not available to a helper" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert allowed == {}
    for name in ("Task", "Agent"):
        nested = asyncio.run(_ask(hook, name, _HELPER_CALL, tool_use_id=f"h1-{name}", agent_id="h1"))
        assert _decision(nested) == "deny", name
    assert state.seen is True
    assert caller["outside_content"] is True


def test_the_pre_tool_hook_applies_its_rules_in_a_fixed_order(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    pre = _hook(_start(_editor(monkeypatch, brain=("web_read",), max_helpers=1)))

    def why(output: dict[str, Any]) -> str:
        return output["hookSpecificOutput"]["permissionDecisionReason"]

    async def scenario():
        nested = await _ask(pre, "Agent", _HELPER_CALL, tool_use_id="h1", agent_id="h1")
        first = await _ask(pre, "Agent", _HELPER_CALL, tool_use_id="a1")
        await _ask(pre, "mcp__brain-tools__web_read", {}, tool_use_id="w1")
        second = await _ask(pre, "Agent", _HELPER_CALL, tool_use_id="a2")
        outside = await _ask(pre, "Read", {"file_path": "/etc/passwd"}, tool_use_id="r1")
        edit = await _ask(pre, "Edit", _change(note), tool_use_id="e1")
        return nested, first, second, outside, edit

    nested, first, second, outside, edit = asyncio.run(scenario())
    assert "not available to a helper" in why(nested)
    assert first == {}
    assert "at most 1 helpers" in why(second)
    assert "confined" in why(outside)
    assert "blocked after untrusted web content" in why(edit)


def test_a_private_run_drafts_a_reply_after_reading_the_mail(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _brain_agent(monkeypatch, ["mail_get_message", "create_draft"])
    fake = FakeQuery(
        _init(["Read", "mcp__brain-tools__mail_get_message", "mcp__brain-tools__create_draft"],
              [{"name": "brain-tools", "status": "connected"}]),
        _result(),
    )
    asyncio.run(ClaudeRunner(query_fn=fake).run(agent, "go", _ctx(areas=None)))
    hook = _hook(fake)

    async def scenario():
        await _ask(hook, "mcp__brain-tools__mail_get_message", {"uid": "1"})
        return await _ask(hook, "mcp__brain-tools__create_draft", {})

    assert _decision(asyncio.run(scenario())) is None


def test_a_refused_tool_call_is_recorded_as_an_event(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre = _hook(_start(_editor(monkeypatch, brain=()), areas=["family"]))

    async def scenario():
        await _ask(pre, "mcp__brain-tools__intake_pending", {}, tool_use_id="p1")
        return await _ask(pre, "mcp__brain-tools__intake_dismiss", {"intake_id": "x"}, tool_use_id="d1")

    refused = asyncio.run(scenario())
    assert _decision(refused) == "deny"
    [event] = _events(isolated_db)
    assert (event["actor"], event["action"], event["subject"]) == ("inbox", "tool_refused", "intake_dismiss")
    assert event["detail"] == refused["hookSpecificOutput"]["permissionDecisionReason"]


def test_events_record_helper_starts_refused_and_direct_edits(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    (vault / "Other").mkdir()
    elsewhere = vault / "Other" / "z.md"
    elsewhere.write_text(_NOTE)
    fake = _start(_editor(monkeypatch, brain=()))
    pre, post = _hook(fake), _post_hook(fake)

    async def scenario():
        await _ask(pre, "Agent", _HELPER_CALL, tool_use_id="a1")
        refused = await _ask(pre, "Edit", _change(elsewhere), tool_use_id="e1")
        await _ask(pre, "Edit", _change(note), tool_use_id="e2")
        note.write_text(_NOTE.replace("83 kg", "82 kg"))
        await _after(post, "Edit", _change(note), tool_use_id="e2")
        return refused

    refused = asyncio.run(scenario())
    assert _decision(refused) == "deny"
    started, refusal, edit = _events(isolated_db)
    [ledger] = _edits(isolated_db)
    assert started["action"] == "helper_start"
    assert started["subject"] == ledger["run_label"]
    assert started["subject"].startswith("inbox@")
    assert refusal["action"] == "edit_refused"
    assert refusal["subject"] == "Other/z.md"
    assert refusal["detail"] == refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert edit["action"] == "direct_edit"
    assert edit["subject"] == "Health/tracker.md"
    for event in (started, refusal, edit):
        assert (event["actor"], event["trust"], event["payload_hash"]) == ("inbox", "system", "")


def test_a_failed_journal_write_does_not_change_the_decision(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    fake = _start(_editor(monkeypatch, brain=()))
    pre, post = _hook(fake), _post_hook(fake)

    def locked(**_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner_mod, "record_event", locked)
    monkeypatch.setattr(runner_mod, "record_vault_edit", locked)

    async def scenario():
        refused = await _ask(pre, "Edit", {**_change(note), "file_path": "/etc/hosts"})
        helper = await _ask(pre, "Agent", _HELPER_CALL)
        allowed = await _ask(pre, "Edit", _change(note), tool_use_id="e1")
        recorded = await _after(post, "Edit", _change(note), tool_use_id="e1")
        return refused, helper, allowed, recorded

    refused, helper, allowed, recorded = asyncio.run(scenario())
    assert _decision(refused) == "deny"
    assert (helper, allowed, recorded) == ({}, {}, {})


def test_feature_switches_drop_tools(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    agent = _editor(monkeypatch, brain=())

    monkeypatch.setenv("BRAIN_DIRECT_EDITS", "0")
    fake = _start(agent)
    options = fake.calls[0]["options"]
    assert options.tools == ["Read", "Agent"]
    assert {"Edit", "Write"} <= set(options.disallowed_tools)
    assert "Agent" not in options.disallowed_tools
    assert options.agents is not None
    assert _decision(asyncio.run(_ask(_hook(fake), "Edit", _change(note)))) == "deny"
    assert asyncio.run(_ask(_hook(fake), "Agent", _HELPER_CALL)) == {}

    monkeypatch.setenv("BRAIN_DIRECT_EDITS", "1")
    monkeypatch.setenv("BRAIN_HELPERS", "0")
    fake = _start(agent)
    options = fake.calls[0]["options"]
    assert options.tools == ["Read", "Edit", "Write"]
    assert "Agent" in options.disallowed_tools
    assert not {"Edit", "Write"} & set(options.disallowed_tools)
    assert options.agents is None
    assert _decision(asyncio.run(_ask(_hook(fake), "Agent", _HELPER_CALL))) == "deny"
    assert asyncio.run(_ask(_hook(fake), "Edit", _change(note))) == {}


def test_a_switched_off_tool_is_not_offered_and_is_refused_by_the_hook(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _brain_agent(monkeypatch, ["store_finding", "draw_chart", "follow_ups_list"])

    monkeypatch.setenv("BRAIN_MEDIA", "0")
    monkeypatch.setenv("BRAIN_FOLLOWUPS", "1")
    fake = _start(agent)
    hook = _hook(fake)
    assert "mcp__brain-tools__draw_chart" not in fake.calls[0]["options"].allowed_tools
    assert "mcp__brain-tools__follow_ups_list" in fake.calls[0]["options"].allowed_tools
    refused = asyncio.run(_ask(hook, "mcp__brain-tools__draw_chart", {}))
    assert _decision(refused) == "deny"
    assert "BRAIN_MEDIA" in refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert _decision(asyncio.run(_ask(hook, "mcp__brain-tools__follow_ups_list", {}))) is None
    assert _decision(asyncio.run(_ask(hook, "mcp__brain-tools__store_finding", {}))) is None

    monkeypatch.setenv("BRAIN_MEDIA", "1")
    monkeypatch.setenv("BRAIN_FOLLOWUPS", "0")
    fake = _start(agent)
    hook = _hook(fake)
    assert "mcp__brain-tools__draw_chart" in fake.calls[0]["options"].allowed_tools
    assert "mcp__brain-tools__follow_ups_list" not in fake.calls[0]["options"].allowed_tools
    refused = asyncio.run(_ask(hook, "mcp__brain-tools__follow_ups_list", {}))
    assert _decision(refused) == "deny"
    assert "BRAIN_FOLLOWUPS" in refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert _decision(asyncio.run(_ask(hook, "mcp__brain-tools__draw_chart", {}))) is None


def test_the_helper_limit_is_capped_at_three() -> None:
    assert runner_mod._grants({"tools": ["Read"], "max_helpers": 3}, {}) == (["Read"], [], 0)
    assert runner_mod._grants({"tools": ["Read", "Agent"], "max_helpers": 7}, {}) == (
        ["Read", "Agent"], [], 3,
    )


@pytest.mark.parametrize(("profile", "daily"), [
    ({"notes": {"daily_notes_dir": "Journal"}}, "Journal"),
    ({"notes": {"folder_name": "vault"}}, "01 - Daily Notes"),
    ({}, "01 - Daily Notes"),
])
def test_offlimits_come_from_the_merged_config(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, Any], daily: str,
) -> None:
    note = _tracker(vault)
    (vault / daily).mkdir()
    (vault / "Rules").mkdir()
    day, rule = vault / daily / "2026-10-15.md", vault / "Rules" / "voice.md"
    day.write_text(_NOTE)
    rule.write_text(_NOTE)
    config = SimpleNamespace(
        profile=profile, vault_instructions={"allowed_roots": ["Rules/"], "agents": {}},
    )
    monkeypatch.setattr("src.brain.runtime.loader.load_all", lambda root: config)
    agent = _editor(monkeypatch, brain=(), vault_paths=[f"{daily}/", "Rules/", "Health/"])
    hook = _hook(_start(agent))

    async def scenario():
        return (
            await _ask(hook, "Edit", _change(day)),
            await _ask(hook, "Edit", _change(rule)),
            await _ask(hook, "Edit", _change(note)),
        )

    daily_note, rules_file, tracker = asyncio.run(scenario())
    for denied in (daily_note, rules_file):
        assert _decision(denied) == "deny"
        assert "off limits" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert tracker == {}


def test_read_roots_from_context_confine_file_tools(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _start(dict(_AGENT), read_roots=[vault / "Shared"])
    hook = _hook(fake)

    async def scenario():
        return (
            await _ask(hook, "Read", {"file_path": str(vault / "Private" / "x.md")}),
            await _ask(hook, "Read", {"file_path": str(vault / "Shared" / "y.md")}),
            await _ask(hook, "Read", {"file_path": "Private/x.md"}),
            await _ask(hook, "Grep", {"pattern": "x"}),
            await _ask(hook, "Read", {"file_path": "Shared/y.md"}),
        )

    private, shared, relative_private, whole_vault, relative_shared = asyncio.run(scenario())
    for denied in (private, relative_private, whole_vault):
        assert _decision(denied) == "deny"
    assert shared == {}
    assert relative_shared == {}


def test_empty_read_roots_leave_no_file_tool_open(
    isolated_db, vault: Path,
) -> None:
    fake = _start(dict(_AGENT), read_roots=[])
    shared = asyncio.run(_ask(_hook(fake), "Read", {"file_path": str(vault / "Shared" / "y.md")}))
    assert _decision(shared) == "deny"


def test_caller_context_carries_outside_content_and_areas(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_server(specs_, allowed, caller_context, **kw):
        captured.append(dict(caller_context))
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    agent = _brain_agent(monkeypatch, ["store_finding"])
    image = {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    _start(
        agent, reply=True, areas=["family", "housing"], vault_paths=["Family/"],
        msg={"images": [image]},
    )
    _start(agent)
    with_image, scheduled = captured
    assert (with_image["outside_content"], with_image["areas"]) == (True, ["family", "housing"])
    assert with_image["vault_paths"] == ["Family/"]
    assert (scheduled["outside_content"], scheduled["areas"]) == (False, None)
    assert scheduled["vault_paths"] is None


def test_an_image_is_outside_content_and_blocks_edits_only_when_unattended(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    captured: dict[str, Any] = {}

    def fake_server(specs_, allowed, caller_context, **kw):
        captured["state"] = kw["state"]
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    note = _tracker(vault)
    fake = _start(_editor(monkeypatch), reply=True, msg={"images": [image]})
    assert captured["state"].seen is True
    assert asyncio.run(_ask(_hook(fake), "Edit", _change(note))) == {}
    fake = _start(_editor(monkeypatch), msg={"images": [image]})
    assert _decision(asyncio.run(_ask(_hook(fake), "Edit", _change(note)))) == "deny"


def test_a_message_that_brings_outside_content_counts_like_an_image(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_server(specs_, allowed, caller_context, **kw):
        captured.append({"caller": dict(caller_context), "state": kw["state"]})
        return {"type": "sdk", "name": "brain-tools", "instance": object()}

    monkeypatch.setattr(runner_mod, "build_brain_server", fake_server)
    note = _tracker(vault)
    chat = _start(_editor(monkeypatch), reply=True, msg={"outside_content": True})
    unattended = _start(_editor(monkeypatch), msg={"outside_content": True})
    plain = _start(_editor(monkeypatch), reply=True, msg={"outside_content": "yes"})
    for run in captured[:2]:
        assert run["caller"]["outside_content"] is True and run["caller"]["untrusted_input"] is True
        assert run["state"].seen is True
    assert captured[2]["caller"]["outside_content"] is False and captured[2]["state"].seen is False
    assert asyncio.run(_ask(_hook(chat), "Edit", _change(note))) == {}
    assert _decision(asyncio.run(_ask(_hook(unattended), "Edit", _change(note)))) == "deny"
    assert asyncio.run(_ask(_hook(plain), "Edit", _change(note))) == {}


@pytest.fixture(autouse=True)
def _fresh_usage_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "USAGE_LIMIT", runner_mod.UsageLimit())


class Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


class SlowQuery(FakeQuery):
    active = 0
    max_active = 0

    def __init__(self, delay: float, *script: Any) -> None:
        super().__init__(*script)
        self.delay = delay

    def __call__(self, *, prompt: Any, options: Any):
        self.calls.append({"prompt": prompt, "options": options})

        async def stream():
            SlowQuery.active += 1
            SlowQuery.max_active = max(SlowQuery.max_active, SlowQuery.active)
            try:
                await asyncio.sleep(self.delay)
                for item in self.script:
                    yield item
            finally:
                SlowQuery.active -= 1

        return stream()


def test_runs_are_serialised(isolated_db, vault: Path) -> None:
    SlowQuery.active = SlowQuery.max_active = 0

    async def scenario():
        runs = [
            ClaudeRunner(query_fn=SlowQuery(0.2, _init(), _result())).run(dict(_AGENT), "go", _ctx())
            for _ in range(2)
        ]
        return await asyncio.gather(*runs)

    results = asyncio.run(scenario())
    assert [r["status"] for r in results] == ["success", "success"]
    assert SlowQuery.max_active == 1


def test_reply_timeout_starts_after_lock(isolated_db, vault: Path) -> None:
    async def scenario():
        long_run = asyncio.create_task(
            ClaudeRunner(query_fn=SlowQuery(1.5, _init(), _result())).run(
                dict(_AGENT), "go", _ctx(timeout=30)),
        )
        await asyncio.sleep(0.05)
        reply = await ClaudeRunner(query_fn=FakeQuery(_init(), _result("hello"))).run_reply(
            "hi", dict(_AGENT), _ctx(timeout=1),
        )
        await long_run
        return reply

    assert asyncio.run(scenario())["status"] == "success"


def _rejected(resets_at: float) -> Any:
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status="rejected", resets_at=int(resets_at)),
        uuid="u", session_id="s",
    )


def test_rate_limit_event_rejected_blocks_and_fails(isolated_db, vault: Path) -> None:
    import time
    notices = Notices()
    fake = FakeQuery(_init(), _rejected(time.time() + 600),
                     _result("limit", subtype="error_during_execution", is_error=True))
    result = asyncio.run(ClaudeRunner(query_fn=fake, notify_fn=notices).run(
        dict(_AGENT), "go", _ctx()))
    assert result["status"] == "failed"
    assert (result["error"] or "").startswith("usage limit reached")
    assert len(notices.sent) == 1
    assert notices.sent[0].startswith("Claude usage limit reached. Agent runs resume after ")
    assert runner_mod.USAGE_LIMIT.active(time.time())


def test_assistant_rate_limit_error_blocks(isolated_db, vault: Path) -> None:
    import time
    limited = AssistantMessage(content=[TextBlock(text="limit")], model="claude-sonnet-5-5",
                               error="rate_limit")
    fake = FakeQuery(_init(), limited, _result("limit", is_error=True))
    result = asyncio.run(ClaudeRunner(query_fn=fake, notify_fn=Notices()).run(
        dict(_AGENT), "go", _ctx()))
    assert (result["error"] or "").startswith("usage limit reached")
    assert runner_mod.USAGE_LIMIT.active(time.time())


def test_blocked_run_skips_without_calling_sdk(isolated_db, vault: Path) -> None:
    import time
    notices = Notices()
    first = FakeQuery(_init(), _rejected(time.time() + 600), _result(None, is_error=True))
    asyncio.run(ClaudeRunner(query_fn=first, notify_fn=notices).run(dict(_AGENT), "go", _ctx()))
    second = FakeQuery(_init(), _result())
    result = asyncio.run(ClaudeRunner(query_fn=second, notify_fn=notices).run(
        dict(_AGENT), "go", _ctx()))
    assert second.calls == []
    assert result["status"] == "failed"
    assert (result["error"] or "").startswith("usage limit reached")
    assert [row["status"] for row in _rows(isolated_db)] == ["failed", "skipped"]
    assert len(notices.sent) == 1


def test_usage_limit_expires_after_reset() -> None:
    limit = runner_mod.UsageLimit()
    now = 1_000_000.0
    assert limit.active(now) is False
    assert limit.block(now + 10, now) is True
    assert limit.block(now + 10, now + 1) is False
    assert limit.active(now + 5) is True
    assert limit.active(now + 11) is False
    assert limit.block(None, now + 20) is True
    assert limit.active(now + 20 + 3599) is True
    assert limit.active(now + 20 + 3601) is False


def test_a_search_the_filter_refuses_does_not_block_later_reads(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _brain_agent(monkeypatch, ["web_search", "calendar_list_events"])
    hook = _hook(_start(agent, reply=True, areas=["family"]))

    async def scenario():
        refused = await _ask(hook, "mcp__brain-tools__web_search", {"query": "cricket schedule"}, tool_use_id="w1")
        calendar = await _ask(hook, "mcp__brain-tools__calendar_list_events", {}, tool_use_id="c1")
        return refused, calendar

    refused, calendar = asyncio.run(scenario())
    assert _decision(refused) == "deny"
    assert "authenticated Slack request" in refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert calendar == {}


def test_a_private_reply_searches_words_the_message_did_not_contain(
    isolated_db, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _brain_agent(monkeypatch, ["web_search"])
    hook = _hook(_start(agent, reply=True, areas=None))

    async def scenario():
        jobs = await _ask(hook, "mcp__brain-tools__web_search", {"query": "solutions architect jobs Germany remote"})
        number = await _ask(hook, "mcp__brain-tools__web_search", {"query": "account 12345678"})
        return jobs, number

    jobs, number = asyncio.run(scenario())
    assert _decision(jobs) is None
    assert "six or more digits" in number["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_direct_edit_keeps_the_previous_version_outside_the_vault(
    isolated_db, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _tracker(vault)
    pre = _hook(_start(_editor(monkeypatch, brain=())))

    assert asyncio.run(_ask(pre, "Edit", _change(note), tool_use_id="e1")) == {}
    [day] = (tmp_path / "user" / "vault-edit-backups").iterdir()
    [copy] = (day / "Health").iterdir()
    assert copy.name.startswith("tracker.md.") and copy.read_text() == _NOTE
    assert copy.stat().st_mode & 0o777 == 0o600


def test_an_edit_whose_previous_version_cannot_be_saved_is_refused(
    isolated_db, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "user" / "vault-edit-backups").write_text("a file where the folder should be")
    note = _tracker(vault)
    pre = _hook(_start(_editor(monkeypatch, brain=())))

    refused = asyncio.run(_ask(pre, "Edit", _change(note), tool_use_id="e1"))
    assert _decision(refused) == "deny"
    assert "previous version could not be saved" in refused["hookSpecificOutput"]["permissionDecisionReason"]
