from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

NOTE = (
    "# Register\n"
    "\n"
    "## Insurance\n"
    "\n"
    "| Item | Due |\n"
    "|---|---|\n"
    "| Liability | 2027-01-01 |\n"
    "\n"
    "## Taxes\n"
    "\n"
    "| Item | Due |\n"
    "|---|---|\n"
    "| Return | 2027-07-31 |\n"
)
ANCHOR = "| Liability | 2027-01-01 |"
TEXT = "| Household | 2027-03-01 |"
AFTER = NOTE.replace(ANCHOR + "\n", ANCHOR + "\n" + TEXT + "\n")


def _insert(existing: str, anchor, text):
    from src.brain.actions.handlers.insert_vault_line import insert_after_anchor

    return insert_after_anchor(existing, anchor, text)


def _write_note(vault, name: str = "Register.md", body: str = NOTE):
    target = vault / name
    target.write_text(body, encoding="utf-8")
    return target


def _payload(**overrides) -> dict:
    return {"path": "Register.md", "anchor": ANCHOR, "text": TEXT, **overrides}


def test_unique_anchor_gets_the_line_directly_after_it() -> None:
    result = _insert(NOTE, ANCHOR, TEXT)

    assert result.content == AFTER
    assert result.anchor_line == 7
    assert result.section == "## Insurance"
    assert result.already_present is False


def test_anchor_without_a_heading_above_has_no_section() -> None:
    result = _insert("first\nsecond\n", "first", "added")

    assert result.section is None
    assert result.content == "first\nadded\nsecond\n"


def test_anchor_matching_no_line_is_rejected_with_the_count() -> None:
    with pytest.raises(ValueError, match="matches 0 lines"):
        _insert(NOTE, "| Liability |", TEXT)


def test_anchor_matching_several_lines_is_rejected_with_line_numbers() -> None:
    with pytest.raises(ValueError, match=r"matches 2 lines \(6, 12\)"):
        _insert(NOTE, "|---|---|", TEXT)


def test_trailing_whitespace_is_ignored_on_either_side() -> None:
    padded_file = NOTE.replace(ANCHOR, ANCHOR + "  \t")

    assert _insert(padded_file, ANCHOR, TEXT).anchor_line == 7
    assert _insert(NOTE, ANCHOR + "   ", TEXT).anchor_line == 7


def test_leading_whitespace_is_not_ignored() -> None:
    with pytest.raises(ValueError, match="matches 0 lines"):
        _insert(NOTE, "  " + ANCHOR, TEXT)


@pytest.mark.parametrize(
    ("anchor", "text"),
    [
        ("| Item | Due |\n|---|---|", TEXT),
        (ANCHOR, "one\ntwo"),
        (ANCHOR, "one\rtwo"),
        (ANCHOR, "one two"),
        (ANCHOR + "\n", TEXT),
        (ANCHOR, ""),
        (ANCHOR, "   "),
        ("", TEXT),
        (ANCHOR, "x" * 2001),
        ("x" * 2001, TEXT),
        (None, TEXT),
        (ANCHOR, 7),
    ],
)
def test_anchor_and_text_must_each_be_one_bounded_line(anchor, text) -> None:
    with pytest.raises(ValueError):
        _insert(NOTE, anchor, text)


def test_trailing_newline_is_kept_as_it_was() -> None:
    assert _insert("a\nb\n", "b", "c").content == "a\nb\nc\n"
    assert _insert("a\nb", "b", "c").content == "a\nb\nc"


def test_line_already_directly_after_the_anchor_is_reported_unchanged() -> None:
    result = _insert(AFTER, ANCHOR, TEXT)

    assert result.already_present is True
    assert result.content == AFTER


# --- propose_action ----------------------------------------------------------


def _routine():
    """An untrusted-input caller shaped like the gateway's scheduled profile."""
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="routine",
        area="assistant",
        allowed_action_types=frozenset({"insert_vault_line"}),
        untrusted_input=True,
    )


def _trusted():
    from src.brain.mcp.caller_context import ToolCaller

    return ToolCaller(
        agent="health",
        area="health",
        allowed_action_types=frozenset({"insert_vault_line"}),
    )


def _propose(caller, **kwargs) -> str:
    from src.brain.mcp.caller_context import caller_scope
    from src.brain.mcp.tools.actions import propose_action

    handler = getattr(propose_action, "handler", propose_action)
    with patch("src.brain.mcp.tools.actions.notifier") as review_notifier:
        review_notifier.return_value.send = AsyncMock(
            return_value={"status": "sent", "id": "1.1"}
        )
        with caller_scope(caller):
            out = asyncio.run(handler(kwargs))
    return out["content"][0]["text"]


def _all_rows() -> list[dict]:
    from src.brain.actions import queue

    statuses = (
        "pending", "approved", "executing", "executed", "partial", "ambiguous",
        "failed", "rejected", "expired",
    )
    return [row for status in statuses for row in queue.list_by_status(status)]


@pytest.mark.parametrize("risk", [None, "auto", "manual", "confirm"])
@pytest.mark.parametrize("caller", [_routine, _trusted])
def test_proposal_is_always_queued_as_confirm(
    isolated_db, isolated_vault, caller, risk
) -> None:
    target = _write_note(isolated_vault)
    args = {"type": "insert_vault_line", "payload": _payload(), "rationale": "new policy"}
    if risk is not None:
        args["risk"] = risk

    text = _propose(caller(), **args)

    assert "risk=confirm" in text
    rows = _all_rows()
    assert [(row["status"], row["risk"]) for row in rows] == [("pending", "confirm")]
    assert json.loads(rows[0]["payload_json"]) == _payload()
    assert target.read_text(encoding="utf-8") == NOTE


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_payload(anchor="| Liability |"), "matches 0 lines"),
        (_payload(anchor="|---|---|"), "matches 2 lines (6, 12)"),
        (_payload(path="Missing.md"), "does not exist"),
        (_payload(expected_sha256="a" * 64), "unsupported fields"),
        (_payload(area="assistant"), "unsupported fields"),
        (_payload(text="one\ntwo"), "one line"),
        (_payload(path="../Register.md"), "normalized, visible .md path"),
        ({"path": "Register.md", "anchor": ANCHOR}, "text"),
    ],
)
def test_rejected_proposal_queues_nothing(
    isolated_db, isolated_vault, payload, expected
) -> None:
    target = _write_note(isolated_vault)

    text = _propose(
        _routine(), type="insert_vault_line", payload=payload, rationale="r"
    )

    assert text.startswith("ERROR:")
    assert expected in text
    assert _all_rows() == []
    assert target.read_text(encoding="utf-8") == NOTE


def test_proposal_for_a_line_already_present_is_rejected(
    isolated_db, isolated_vault
) -> None:
    _write_note(isolated_vault, body=AFTER)

    text = _propose(
        _routine(), type="insert_vault_line", payload=_payload(), rationale="r"
    )

    assert text.startswith("ERROR:")
    assert "already present" in text
    assert _all_rows() == []


def test_three_proposals_against_one_note_all_stay_approvable_after_the_first_runs(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.approval_review import build_approval_review

    target = _write_note(isolated_vault)
    texts = [TEXT, "| Legal cover | 2027-04-01 |", "| Car | 2027-05-01 |"]
    for text in texts:
        assert "queued" in _propose(
            _trusted(),
            type="insert_vault_line",
            payload=_payload(text=text),
            rationale="r",
        )
    pending = queue.list_by_status("pending")
    assert len(pending) == 3

    for row in pending:
        assert build_approval_review(queue.get(row["id"])).approvable
        queue.record_review_delivery(
            row["id"], row["payload_hash"], "health", "CHEALTH", "1.2"
        )
        assert queue.transition_status(
            row["id"], "pending", "approved", channel="slack_reply", actor="U1"
        )
        asyncio.run(executor.drain_once())
        assert queue.get(row["id"])["status"] == "executed"

    lines = target.read_text(encoding="utf-8").split("\n")
    assert sorted(set(lines) - set(NOTE.split("\n"))) == sorted(texts)
    assert len(lines) == len(NOTE.split("\n")) + 3


# --- approval review ---------------------------------------------------------


def _row(payload: dict | None = None) -> dict:
    from src.brain.actions.types import Action, ActionType

    return Action(
        id="abcdef123456",
        type=ActionType.INSERT_VAULT_LINE,
        payload=payload or _payload(),
        rationale="track the household policy",
    ).to_row()


def test_review_shows_target_section_line_number_anchor_and_added_text(
    isolated_vault,
) -> None:
    from src.brain.actions.approval_review import build_approval_review

    _write_note(isolated_vault)

    review = build_approval_review(_row())

    assert review.approvable is True
    assert review.heading == "Add line to note"
    assert review.summary_lines == (
        "Effect: Add one line; nothing else in the note changes",
        "Target: Register.md",
        "Section: ## Insurance",
        f"After line 7: {ANCHOR}",
        f"Add: {TEXT}",
    )


def test_review_omits_the_section_line_when_no_heading_is_above(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review

    _write_note(isolated_vault, body="first\nsecond\n")

    review = build_approval_review(_row(_payload(anchor="first", text="added")))

    assert review.summary_lines == (
        "Effect: Add one line; nothing else in the note changes",
        "Target: Register.md",
        "After line 1: first",
        "Add: added",
    )


def test_review_escapes_slack_tokens_in_anchor_and_text(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review

    _write_note(isolated_vault, body="## A <b>\nrow <!here>\n")

    review = build_approval_review(
        _row(_payload(anchor="row <!here>", text="<!channel> & more"))
    )

    assert review.approvable is True
    rendered = "\n".join(review.summary_lines)
    assert "<" not in rendered and ">" not in rendered and "&" not in rendered


@pytest.mark.parametrize(
    ("later_body", "reason"),
    [
        (NOTE.replace(ANCHOR, "| Liability | 2028-01-01 |"), "matches 0 lines"),
        (NOTE + ANCHOR + "\n", "matches 2 lines (7, 14)"),
        (AFTER, "already present"),
    ],
)
def test_review_is_not_approvable_once_the_anchor_rule_no_longer_holds(
    isolated_vault, later_body, reason
) -> None:
    from src.brain.actions.approval_review import build_approval_review

    _write_note(isolated_vault, body=later_body)

    review = build_approval_review(_row())

    assert review.approvable is False
    assert reason in review.reason


def test_review_is_not_approvable_when_the_note_is_gone(isolated_vault) -> None:
    from src.brain.actions.approval_review import build_approval_review

    review = build_approval_review(_row())

    assert review.approvable is False


@pytest.mark.parametrize(
    "payload",
    [
        _payload(expected_sha256=None),
        _payload(text="one\ntwo"),
        _payload(path=".obsidian/Register.md"),
        {"path": "Register.md", "anchor": ANCHOR},
    ],
)
def test_review_rejects_a_queued_payload_outside_the_schema(
    isolated_vault, payload
) -> None:
    from src.brain.actions.approval_review import build_approval_review

    _write_note(isolated_vault)

    assert build_approval_review(_row(payload)).approvable is False


def test_human_approval_is_refused_once_the_anchor_is_gone(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    target = _write_note(isolated_vault)
    queue.insert(Action(
        id="abcdef123456",
        type=ActionType.INSERT_VAULT_LINE,
        payload=_payload(),
        rationale="r",
    ))
    target.write_text(NOTE.replace(ANCHOR, "rewritten"), encoding="utf-8")

    assert queue.transition_status(
        "abcdef123456", "pending", "approved", channel="slack_button", actor="U1"
    ) is False
    assert queue.get("abcdef123456")["status"] == "pending"


# --- handler -----------------------------------------------------------------


def _execute(payload: dict) -> dict:
    from src.brain.actions.handlers.insert_vault_line import execute

    return asyncio.run(execute(payload))


def test_handler_changes_exactly_one_line(isolated_vault) -> None:
    target = _write_note(isolated_vault)

    result = _execute(_payload())

    assert result["ok"] is True
    after = target.read_text(encoding="utf-8")
    assert after == AFTER
    before_lines, after_lines = NOTE.split("\n"), after.split("\n")
    assert len(after_lines) == len(before_lines) + 1
    assert after_lines[:7] + after_lines[8:] == before_lines
    assert after_lines[7] == TEXT


def test_handler_second_run_is_a_no_op(isolated_vault) -> None:
    target = _write_note(isolated_vault)
    assert _execute(_payload())["ok"] is True

    result = _execute(_payload())

    assert result == {"ok": True, "result": "unchanged (idempotent no-op)"}
    assert target.read_text(encoding="utf-8") == AFTER


def test_handler_keeps_a_missing_trailing_newline_missing(isolated_vault) -> None:
    target = _write_note(isolated_vault, body="a\nb")

    assert _execute(_payload(anchor="b", text="c"))["ok"] is True

    assert target.read_text(encoding="utf-8") == "a\nb\nc"


@pytest.mark.parametrize(
    ("anchor", "reason"),
    [("| Liability |", "matches 0 lines"), ("|---|---|", "matches 2 lines (6, 12)")],
)
def test_handler_leaves_the_file_untouched_when_the_anchor_rule_fails(
    isolated_vault, anchor, reason
) -> None:
    target = _write_note(isolated_vault)

    result = _execute(_payload(anchor=anchor))

    assert result["ok"] is False
    assert reason in result["result"]
    assert not result.get("retryable")
    assert target.read_text(encoding="utf-8") == NOTE


def test_handler_does_not_create_a_missing_note(isolated_vault) -> None:
    result = _execute(_payload(path="Missing.md"))

    assert result["ok"] is False
    assert not result.get("retryable")
    assert not (isolated_vault / "Missing.md").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        ".obsidian/plugins/agent/main.md",
        "Notes/settings.json",
        "Notes/../Register.md",
        "/etc/hosts.md",
    ],
)
def test_handler_rejects_unsafe_paths(isolated_vault, path) -> None:
    result = _execute(_payload(path=path))

    assert result["ok"] is False
    assert "unsafe vault note path" in result["result"]
    assert not result.get("retryable")


def test_handler_reports_a_concurrent_edit_as_retryable(
    isolated_vault, monkeypatch
) -> None:
    from src.brain.actions.handlers import insert_vault_line

    target = _write_note(isolated_vault)
    real_read = insert_vault_line.read_regular_text

    def read_then_human_edits(path):
        text = real_read(path)
        target.write_text(NOTE + "typed by a human meanwhile\n", encoding="utf-8")
        return text

    monkeypatch.setattr(insert_vault_line, "read_regular_text", read_then_human_edits)

    result = _execute(_payload())

    assert result["ok"] is False
    assert result["retryable"] is True
    assert target.read_text(encoding="utf-8") == NOTE + "typed by a human meanwhile\n"


# --- executor and policy -----------------------------------------------------


def _queue_approved(channel: str, actor: str | None) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType, Risk

    queue.insert(Action(
        id="abcdef123456",
        type=ActionType.INSERT_VAULT_LINE,
        payload=_payload(),
        rationale="r",
        risk=Risk.AUTO,
    ))
    assert queue.transition_status(
        "abcdef123456", "pending", "approved", channel=channel, actor=actor
    )


@pytest.mark.parametrize(
    ("channel", "actor"),
    [("auto", None), ("trusted_client", "external-mcp"), ("slack_button", None)],
)
def test_executor_refuses_the_type_without_a_human_approval_channel(
    isolated_db, isolated_vault, channel, actor
) -> None:
    from src.brain.actions import executor, queue

    target = _write_note(isolated_vault)
    # The last case: a human channel name without an actor is not an approval.
    _queue_approved(channel, actor)

    asyncio.run(executor.drain_once())

    row = queue.get("abcdef123456")
    assert row["status"] == "failed"
    assert "refused" in row["result"]
    assert target.read_text(encoding="utf-8") == NOTE


def test_executor_runs_the_type_after_a_human_approval(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import executor, queue

    target = _write_note(isolated_vault)
    _queue_approved("slack_button", "U1")

    asyncio.run(executor.drain_once())

    assert queue.get("abcdef123456")["status"] == "executed"
    assert target.read_text(encoding="utf-8") == AFTER


def test_type_is_registered_and_confirm_only() -> None:
    from pathlib import Path

    import yaml

    from src.brain.actions.handlers import HANDLERS, insert_vault_line
    from src.brain.actions.types import (
        AUTO_ELIGIBLE_TYPES,
        OWN_DEVICE_AUTO_TYPES,
        TRUSTED_CLIENT_ACTION_TYPES,
        ActionType,
    )
    from src.brain.runtime.loader import LoadedConfig, load_agents
    from src.orchestrator.chat import chat_run_config
    from src.orchestrator.registry import _build_config

    repo_root = Path(__file__).resolve().parents[1]
    atype = ActionType.INSERT_VAULT_LINE
    assert atype.value == "insert_vault_line"
    assert HANDLERS[atype] is insert_vault_line.execute
    assert atype not in AUTO_ELIGIBLE_TYPES
    assert atype not in OWN_DEVICE_AUTO_TYPES
    assert atype not in TRUSTED_CLIENT_ACTION_TYPES
    shipped, _, _ = _build_config(
        LoadedConfig(repo_root=repo_root, agents=load_agents(repo_root / "config"))
    )
    for place in ("shared", "private"):
        chat = chat_run_config(place, thread_area=None, text="hello", registry=shipped)
        assert atype.value not in chat["allowed_action_types"]
    policy = yaml.safe_load(
        (repo_root / "config" / "risk_policy.yaml").read_text(encoding="utf-8")
    )
    assert policy["defaults"]["insert_vault_line"] == "confirm"
