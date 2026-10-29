from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from src.brain.mcp.caller_context import parse_caller_context
from src.orchestrator.runners import brain_tool_bridge as bridge
from src.orchestrator.runners.brain_tool_bridge import (
    ApprovalTracker,
    ReplyDeliveryTracker,
    bridge_call,
    make_brain_tools,
    require_known_brain_tools,
)
from src.orchestrator.runners.run_guard import OutsideContentState, WebRiskBarrier


def _result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"is_error": is_error, "content": [{"type": "text", "text": text}]}


def test_records_a_sent_reply_result() -> None:
    tracker = ReplyDeliveryTracker()
    tracker.observe("reply_to_message", _result("reply:sent id=123.456"))
    assert tracker.delivered is True


@pytest.mark.parametrize(("name", "is_error", "text"), [
    ("reply_to_message", False, "reply:noop id=None"),
    ("reply_to_message", False, "reply:failed id=None"),
    ("reply_to_message", False, "reply failed: timeout"),
    ("reply_to_message", True, "reply:sent id=123.456"),
    ("store_finding", False, "reply:sent id=123.456"),
])
def test_does_not_record_other_results(name: str, is_error: bool, text: str) -> None:
    tracker = ReplyDeliveryTracker()
    tracker.observe(name, _result(text, is_error))
    assert tracker.delivered is False


def test_attaches_queued_action_ids_without_writing_them_into_the_reply() -> None:
    tracker = ApprovalTracker()
    tracker.observe([
        {"type": "text", "text": "action abc123def456 queued (type=mail_archive, risk=confirm)"},
        {"type": "text", "text": "action fed654cba321 already pending (dedup match) — skipped"},
    ])
    assert tracker.enrich_reply({"text": "Both are ready.", "area": "inbox"}) == {
        "text": "Both are ready.",
        "area": "inbox",
        "proposal_action_ids": ["abc123def456", "fed654cba321"],
    }


def test_does_not_duplicate_an_id_already_present_in_the_reply() -> None:
    tracker = ApprovalTracker()
    tracker.observe([{"type": "text", "text": "action abc123def456 queued (type=x, risk=confirm)"}])
    assert tracker.enrich_reply({"text": "Approve `abc123def456`."}) == {
        "text": "Approve `abc123def456`.",
        "proposal_action_ids": ["abc123def456"],
    }


def test_merges_observed_ids_with_explicit_approval_attachments() -> None:
    tracker = ApprovalTracker()
    tracker.observe([{"type": "text", "text": "action abc123def456 queued (type=x, risk=confirm)"}])
    assert tracker.enrich_reply({
        "text": "Ready.",
        "proposal_action_ids": ["fed654cba321", "abc123def456"],
    }) == {
        "text": "Ready.",
        "proposal_action_ids": ["fed654cba321", "abc123def456"],
    }


def test_leaves_unrelated_replies_unchanged() -> None:
    tracker = ApprovalTracker()
    params = {"text": "Nothing to approve."}
    assert tracker.enrich_reply(params) is params


_SPECS = [{"name": "store_finding", "description": "store", "input_schema": {}}]


def test_keeps_an_exact_known_allowlist() -> None:
    assert require_known_brain_tools(["store_finding"], _SPECS) == ["store_finding"]


def test_fails_closed_when_a_requested_tool_is_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown brain tool\\(s\\): missing_tool"):
        require_known_brain_tools(["store_finding", "missing_tool"], _SPECS)


def _fixture_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    script = tmp_path / "fake_bridge.py"
    script.write_text(textwrap.dedent(body))
    monkeypatch.setattr(bridge, "BRIDGE_COMMAND", [sys.executable, str(script)])
    return script


def test_bridge_call_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_bridge(tmp_path, monkeypatch, """
        import json, sys
        assert sys.argv[1] == "call"
        request = json.load(sys.stdin)
        print(json.dumps({"is_error": False, "content": [
            {"type": "text", "text": request["name"] + ":" + request["caller_context"]["agent"]}
        ]}))
    """)
    result = asyncio.run(bridge_call("mail_search", {"q": "x"}, ["mail_search"], {"agent": "inbox"}))
    assert result == {"is_error": False, "content": [{"type": "text", "text": "mail_search:inbox"}]}


def test_bridge_call_kills_process_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "pid"
    _fixture_bridge(tmp_path, monkeypatch, f"""
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        time.sleep(30)
    """)
    with pytest.raises(TimeoutError):
        asyncio.run(bridge_call("mail_search", {}, ["mail_search"], {}, timeout_s=1.0))
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_bridge_call_rejects_output_over_1mb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_bridge(tmp_path, monkeypatch, """
        import sys
        sys.stdout.write("x" * 1_100_000)
    """)
    with pytest.raises(RuntimeError, match="1MB"):
        asyncio.run(bridge_call("mail_search", {}, ["mail_search"], {}))


class _FakeBridge:
    def __init__(self, text: str = "ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self.text = text

    async def __call__(self, name, arguments, allowed, caller_context, **_kw):
        self.calls.append({"name": name, "arguments": arguments, "allowed": allowed,
                           "caller_context": dict(caller_context)})
        return {"is_error": False, "content": [{"type": "text", "text": self.text}]}


def _tools(monkeypatch, fake, names, barrier=None, approvals=None, replies=None):
    monkeypatch.setattr(bridge, "bridge_call", fake)
    specs = [
        {"name": n, "description": f"{n} tool", "input_schema": {"type": "object"}}
        for n in names
    ]
    caller: dict[str, Any] = {"agent": "inbox", "untrusted_input": False}
    tools = make_brain_tools(
        specs, list(names), caller,
        barrier=barrier or WebRiskBarrier(caller),
        approvals=approvals or ApprovalTracker(),
        replies=replies or ReplyDeliveryTracker(),
    )
    return {t.name: t for t in tools}, caller


def test_server_tool_forces_confirm_after_web(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBridge()
    caller: dict[str, Any] = {"untrusted_input": False}
    barrier = WebRiskBarrier(caller)
    tools, _ = _tools(monkeypatch, fake, ["web_search", "propose_action"], barrier=barrier)
    assert barrier.check("web_search") is None
    asyncio.run(tools["propose_action"].handler({"type": "x", "risk": "auto"}))
    assert fake.calls[0]["arguments"]["risk"] == "confirm"


def test_verify_mode_forces_confirm_on_every_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBridge()
    monkeypatch.setattr(bridge, "bridge_call", fake)
    caller: dict[str, Any] = {"untrusted_input": False}
    specs = [{"name": "propose_action", "description": "p", "input_schema": {}}]
    tools = make_brain_tools(
        specs, ["propose_action"], caller, barrier=WebRiskBarrier(caller),
        approvals=ApprovalTracker(), replies=ReplyDeliveryTracker(), verify=True,
    )
    asyncio.run(tools[0].handler({"type": "create_reminder", "risk": "auto"}))
    assert fake.calls[0]["arguments"]["risk"] == "confirm"


def test_server_tool_enriches_reply_with_pending_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBridge("action abc123def456 queued (type=x, risk=confirm)")
    replies = ReplyDeliveryTracker()
    tools, _ = _tools(monkeypatch, fake, ["propose_action", "reply_to_message"], replies=replies)
    asyncio.run(tools["propose_action"].handler({"type": "x"}))
    fake.text = "reply:sent id=1.2"
    result = asyncio.run(tools["reply_to_message"].handler({"text": "Done."}))
    sent = fake.calls[1]["arguments"]
    assert sent["text"] == "Done."
    assert sent["proposal_action_ids"] == ["abc123def456"]
    assert result == {"content": [{"type": "text", "text": "reply:sent id=1.2"}], "is_error": False}
    assert replies.delivered is True


def test_server_tool_sends_live_caller_context(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBridge()
    tools, caller = _tools(monkeypatch, fake, ["store_finding"])
    caller["untrusted_input"] = True
    asyncio.run(tools["store_finding"].handler({}))
    assert fake.calls[0]["caller_context"]["untrusted_input"] is True
    assert fake.calls[0]["allowed"] == ["store_finding"]


def test_server_tool_keeps_schema_without_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _ = _tools(monkeypatch, _FakeBridge(), ["store_finding"])
    assert tools["store_finding"].input_schema == {"type": "object", "properties": {}}


def test_only_allowed_specs_become_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "bridge_call", _FakeBridge())
    specs = [{"name": n, "description": n, "input_schema": {}} for n in ["a", "b"]]
    caller: dict[str, Any] = {}
    tools = make_brain_tools(
        specs, ["a"], caller, barrier=WebRiskBarrier(caller),
        approvals=ApprovalTracker(), replies=ReplyDeliveryTracker(),
    )
    assert [t.name for t in tools] == ["a"]


_ENVELOPE = {
    "agent": "inbox", "area": "inbox", "allowed_action_types": [],
    "direct_action_types": [], "is_reply": False, "untrusted_input": False,
}


def test_parse_caller_context_reads_outside_content_and_areas() -> None:
    caller = parse_caller_context(
        {**_ENVELOPE, "outside_content": True, "areas": ["family", "housing"]},
    )
    assert caller is not None
    assert caller.outside_content is True
    assert caller.areas == frozenset({"family", "housing"})

    plain = parse_caller_context(_ENVELOPE)
    assert plain is not None
    assert (plain.outside_content, plain.areas) == (False, None)
    every_area = parse_caller_context({**_ENVELOPE, "outside_content": False, "areas": None})
    assert every_area is not None
    assert every_area.areas is None

    assert parse_caller_context({**_ENVELOPE, "areas": ["", "x"]}) is None


@pytest.mark.parametrize("extra", [
    {"outside_content": "yes"},
    {"outside_content": None},
    {"areas": "family"},
    {"areas": [7]},
    {"areas": [" "]},
    {"areas": ["x" * 101]},
    {"areas": ["a"] * 101},
])
def test_caller_context_rejects_malformed_outside_content_and_areas(extra: dict[str, Any]) -> None:
    assert parse_caller_context({**_ENVELOPE, **extra}) is None


class _FlaggedBridge:
    async def __call__(self, name, arguments, allowed, caller_context, **_kw):
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "a flagged finding"}],
            "outside_content": True,
        }


@pytest.mark.parametrize(("unattended", "seen"), [(True, True), (False, False)],
                         ids=["unattended", "chat"])
def test_tool_result_flag_marks_outside_content_in_unattended_runs(
    monkeypatch: pytest.MonkeyPatch, unattended: bool, seen: bool,
) -> None:
    monkeypatch.setattr(bridge, "bridge_call", _FlaggedBridge())
    caller: dict[str, Any] = {"agent": "inbox", "untrusted_input": False}
    state = OutsideContentState(caller, unattended=unattended)
    [tool] = make_brain_tools(
        [{"name": "get_recent_findings", "description": "f", "input_schema": {}}],
        ["get_recent_findings"], caller,
        barrier=WebRiskBarrier(caller), approvals=ApprovalTracker(),
        replies=ReplyDeliveryTracker(), state=state,
    )
    result = asyncio.run(tool.handler({}))
    assert state.seen is seen
    assert "outside_content" not in result
    assert result == {"content": [{"type": "text", "text": "a flagged finding"}], "is_error": False}


class _FlaggedFindings:
    def __init__(self) -> None:
        self.contexts: dict[str, dict[str, Any]] = {}

    async def __call__(self, name, arguments, allowed, caller_context, **_kw):
        self.contexts[name] = dict(caller_context)
        result: dict[str, Any] = {"is_error": False, "content": [{"type": "text", "text": name}]}
        if name == "get_recent_findings":
            result["outside_content"] = True
        return result


@pytest.mark.parametrize("unattended", [True, False], ids=["unattended", "chat"])
def test_a_finding_stored_after_reading_a_flagged_one_is_flagged_too(
    monkeypatch: pytest.MonkeyPatch, unattended: bool,
) -> None:
    fake = _FlaggedFindings()
    monkeypatch.setattr(bridge, "bridge_call", fake)
    caller: dict[str, Any] = {"agent": "family", "untrusted_input": False, "outside_content": False}
    state = OutsideContentState(caller, unattended=unattended)
    names = ["get_recent_findings", "store_finding"]
    tools = {tool.name: tool for tool in make_brain_tools(
        [{"name": name, "description": name, "input_schema": {}} for name in names],
        names, caller, barrier=WebRiskBarrier(caller), approvals=ApprovalTracker(),
        replies=ReplyDeliveryTracker(), state=state,
    )}

    asyncio.run(tools["get_recent_findings"].handler({}))
    asyncio.run(tools["store_finding"].handler({"title": "a summary of it"}))

    assert fake.contexts["get_recent_findings"]["outside_content"] is False
    assert fake.contexts["store_finding"]["outside_content"] is True
    assert state.blocks_edits is unattended


def test_build_brain_server_hands_the_state_to_the_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    def spy(specs, allowed, caller_context, **kw):
        received.update(kw)
        return []

    monkeypatch.setattr(bridge, "make_brain_tools", spy)
    caller: dict[str, Any] = {}
    state = OutsideContentState(caller, unattended=True)
    bridge.build_brain_server(
        [], [], caller, barrier=WebRiskBarrier(caller), approvals=ApprovalTracker(),
        replies=ReplyDeliveryTracker(), state=state,
    )
    assert received["state"] is state


@pytest.mark.parametrize("name, args", [
    ("propose_action", {"type": "mail_archive"}),
    ("mail_archive", {}),
])
def test_a_run_proposes_at_most_three_mail_archives(monkeypatch: pytest.MonkeyPatch, name, args) -> None:
    from src.orchestrator.runners.brain_tool_bridge import TOO_MANY_ARCHIVES

    fake = _FakeBridge("action abc123def456 queued (type=mail_archive, risk=confirm)")
    tools, _ = _tools(monkeypatch, fake, [name])
    results = [asyncio.run(tools[name].handler(dict(args))) for _ in range(4)]

    assert [r["is_error"] for r in results] == [False, False, False, True]
    assert results[3]["content"][0]["text"] == TOO_MANY_ARCHIVES
    assert len(fake.calls) == 3


def test_other_proposals_are_not_counted_against_the_archive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBridge("action abc123def456 queued (type=create_reminder, risk=confirm)")
    tools, _ = _tools(monkeypatch, fake, ["propose_action"])
    results = [asyncio.run(tools["propose_action"].handler({"type": "create_reminder"})) for _ in range(5)]

    assert not any(r["is_error"] for r in results)
