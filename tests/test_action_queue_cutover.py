from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch


def _call(tool, **kwargs):
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    handler = getattr(tool, "handler", tool)
    with caller_scope(ToolCaller(
        agent="test",
        area="test",
        allowed_action_types=frozenset({
            "push_notification",
            "create_todoist_task",
        }),
    )):
        return asyncio.run(handler(kwargs))


def test_push_notify_writes_to_queue_and_executes(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.notifications import push_notify

    with patch("src.brain.actions.handlers.push_notification.notifier") as nfac:
        ninst = AsyncMock()
        ninst.send = AsyncMock(return_value={"status": "noop"})
        ninst.persona_for = lambda *a, **k: None
        nfac.return_value = ninst

        out = _call(
            push_notify,
            title="Hello",
            message="World",
            agent="test",
            proposal_action_ids=["abc123def456"],
        )

    assert "executed" in out["content"][0]["text"].lower()
    rows = queue.list_by_status("executed")
    assert len(rows) == 1
    assert rows[0]["type"] == "push_notification"
    assert rows[0]["approval_channel"] == "auto"
    assert json.loads(rows[0]["payload_json"])["proposal_action_ids"] == [
        "abc123def456"
    ]
    assert ninst.send.await_args.kwargs["proposal_action_ids"] == ["abc123def456"]


def test_tasks_create_writes_to_queue_and_dispatches_handler(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.mcp.tools.tasks import tasks_create

    fake_tasks = AsyncMock()
    fake_tasks.list_pending = AsyncMock(return_value=[])
    fake_tasks.list_completed = AsyncMock(return_value=[])
    fake_tasks.add = AsyncMock(return_value={"id": "999", "content": "Smoke task"})

    with patch("src.brain.actions.handlers.create_todoist_task.tasks", return_value=fake_tasks):
        out = _call(
            tasks_create,
            content="Smoke task",
            project_id="p1",
        )

    assert "executed" in out["content"][0]["text"].lower()
    fake_tasks.add.assert_awaited_once()
    rows = queue.list_by_status("executed")
    assert len(rows) == 1
    assert rows[0]["type"] == "create_todoist_task"


def test_action_note_task_link_requires_approval_then_records_backend_id(
    isolated_db,
) -> None:
    from src.brain.actions import approvals, executor, queue
    from src.brain.mcp.tools.tasks import tasks_create

    fake_tasks = AsyncMock()
    fake_tasks.list_pending = AsyncMock(return_value=[])
    fake_tasks.list_completed = AsyncMock(return_value=[])
    fake_tasks.add = AsyncMock(return_value={"id": "999", "content": "Linked task"})

    out = _call(
        tasks_create,
        content="Linked task",
        project_id="p1",
        action_note_id="A-20260819-01",
    )

    text = out["content"][0]["text"]
    assert "queued" in text.lower()
    assert "executed" not in text.lower()
    row = queue.list_by_status("pending")[0]
    assert row["risk"] == "confirm"
    queue.record_review_delivery(
        row["id"], row["payload_hash"], "test", "CTEST", "1.1"
    )
    assert approvals.handle_reply(
        f"yes {row['id']}", area="test", actor="UOWNER", owner=True
    ) is True

    with patch(
        "src.brain.actions.handlers.create_todoist_task.tasks",
        return_value=fake_tasks,
    ):
        asyncio.run(executor.drain_once())

    assert queue.get(row["id"])["status"] == "executed"
    fake_tasks.add.assert_awaited_once()
    linked = isolated_db.get_db().execute(
        "SELECT task_id FROM action_note_task_links WHERE action_id=?",
        ("A-20260819-01",),
    ).fetchone()
    assert linked == ("999",)


def test_push_tools_require_a_delivery_target() -> None:
    from src.orchestrator.registry import AGENT_REGISTRY

    push_tools = {
        "mcp__brain-tools__push_notify",
        "mcp__brain-tools__reply_to_message",
    }
    for name, cfg in AGENT_REGISTRY.items():
        tools = set(cfg["tools"])
        has_delivery = bool(cfg.get("delivery", {}).get("slack")) if cfg.get("delivery") else False
        if not has_delivery:
            assert not (push_tools & tools), (
                f"agent '{name}' has no delivery.slack but still has push tools: "
                f"{push_tools & tools}"
            )
