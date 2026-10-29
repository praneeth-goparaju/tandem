from __future__ import annotations

import asyncio
import json
import threading

import pytest

from src.brain.actions import executor, queue
from src.brain.actions.types import Action, ActionType, Risk


def _insert_approved(action_id: str = "a1", type_: ActionType = ActionType.PUSH_NOTIFICATION,
                     payload: dict | None = None) -> dict:
    queue.insert(Action(
        id=action_id,
        type=type_,
        payload=payload or {"title": "t", "message": "m"},
        rationale="why",
        risk=Risk.CONFIRM,
    ))
    queue.transition_status(action_id, "pending", "approved")
    return queue.get(action_id)


def test_transition_pending_to_approved_sets_decided_at(isolated_db) -> None:
    queue.insert(Action(id="a1", type=ActionType.PUSH_NOTIFICATION, payload={}, rationale="r"))
    assert queue.transition_status("a1", "pending", "approved") is True
    row = queue.get("a1")
    assert row["status"] == "approved"
    assert row["decided_at"] is not None
    assert row["executed_at"] is None


def test_transition_approved_to_executed_sets_executed_at(isolated_db) -> None:
    _insert_approved("a1")
    assert queue.transition_status("a1", "approved", "executed", result="ok") is True
    row = queue.get("a1")
    assert row["status"] == "executed"
    assert row["executed_at"] is not None
    assert row["result"] == "ok"


def test_transition_approved_to_failed_sets_executed_at(isolated_db) -> None:
    _insert_approved("a1")
    assert queue.transition_status("a1", "approved", "failed", result="boom") is True
    row = queue.get("a1")
    assert row["status"] == "failed"
    assert row["executed_at"] is not None
    assert row["result"] == "boom"


def test_transition_from_wrong_state_returns_false(isolated_db) -> None:
    _insert_approved("a1")
    queue.transition_status("a1", "approved", "executed", result="ok")
    assert queue.transition_status("a1", "approved", "executed") is False
    row = queue.get("a1")
    assert row["status"] == "executed"
    assert row["result"] == "ok"


def test_transition_unknown_id_returns_false(isolated_db) -> None:
    assert queue.transition_status("does-not-exist", "approved", "executed") is False


@pytest.fixture
def stub_handlers(monkeypatch):
    calls: list[dict] = []

    def _factory(behavior):
        async def _handler(payload):
            calls.append(payload)
            if behavior == "ok":
                return {"ok": True, "result": "did the thing"}
            if behavior == "fail":
                return {"ok": False, "result": "handler said no"}
            if behavior == "retryable":
                return {"ok": False, "result": "service unavailable", "retryable": True}
            if behavior == "partial":
                return {"ok": True, "result": "root sent; detail failed", "partial": True}
            if behavior == "ambiguous":
                return {"ok": False, "result": "provider outcome unknown", "ambiguous": True}
            if behavior == "invalid":
                return None
            if behavior == "raise":
                raise RuntimeError("kaboom")
            if behavior == "timeout":
                await asyncio.sleep(10)
                return {"ok": True}
            if behavior == "handler_timeout":
                raise TimeoutError("provider deadline")
            raise AssertionError(f"unknown behavior {behavior}")
        return _handler

    def _install(behavior):
        monkeypatch.setitem(executor.HANDLERS, ActionType.PUSH_NOTIFICATION, _factory(behavior))
        return calls

    return _install


def test_run_one_happy_path_marks_executed(isolated_db, stub_handlers) -> None:
    calls = stub_handlers("ok")
    row = _insert_approved("a1", payload={"x": 1})
    asyncio.run(executor._run_one(row))
    assert calls == [{
        "x": 1,
        "_action_id": "a1",
        "_action_created_at": row["created_at"],
        "_origin_agent": None,
        "_origin_area": None,
    }]
    final = queue.get("a1")
    assert final["status"] == "executed"
    assert final["result"] == "did the thing"


def test_run_one_handler_returns_ok_false_marks_failed(isolated_db, stub_handlers) -> None:
    stub_handlers("fail")
    row = _insert_approved("a1")
    asyncio.run(executor._run_one(row))
    final = queue.get("a1")
    assert final["status"] == "failed"
    assert final["result"] == "handler said no"


def test_run_one_retryable_result_requeues_then_exhausts(isolated_db, stub_handlers, monkeypatch) -> None:
    monkeypatch.setattr(executor, "MAX_RETRIES", 1)
    stub_handlers("retryable")
    row = _insert_approved("a1")

    asyncio.run(executor._run_one(row))
    after_first = queue.get("a1")
    assert after_first["status"] == "approved"
    assert after_first["retry_count"] == 1
    assert "service unavailable" in after_first["result"]

    asyncio.run(executor._run_one(after_first))
    final = queue.get("a1")
    assert final["status"] == "failed"
    assert final["retry_count"] == 1
    assert final["result"] == "service unavailable"


def test_run_one_partial_success_is_terminal_and_visible(isolated_db, stub_handlers) -> None:
    stub_handlers("partial")
    row = _insert_approved("a1")

    asyncio.run(executor._run_one(row))

    final = queue.get("a1")
    assert final["status"] == "partial"
    assert final["result"] == "root sent; detail failed"
    assert final["executed_at"] is not None


def test_run_one_ambiguous_handler_result_is_terminal(isolated_db, stub_handlers) -> None:
    stub_handlers("ambiguous")
    row = _insert_approved("a1")

    asyncio.run(executor._run_one(row))

    final = queue.get("a1")
    assert final["status"] == "ambiguous"
    assert final["retry_count"] == 0
    assert final["result"] == "provider outcome unknown"


def test_run_one_invalid_handler_result_fails_instead_of_sticking(
    isolated_db, stub_handlers
) -> None:
    stub_handlers("invalid")
    row = _insert_approved("a1")

    asyncio.run(executor._run_one(row))

    final = queue.get("a1")
    assert final["status"] == "failed"
    assert "invalid handler result" in final["result"]


def test_run_one_invalid_stored_json_fails_instead_of_sticking(
    isolated_db, stub_handlers
) -> None:
    calls = stub_handlers("ok")
    row = _insert_approved("a1")
    row["payload_json"] = "{not-json"

    asyncio.run(executor._run_one(row))

    final = queue.get("a1")
    assert final["status"] == "failed"
    assert "invalid stored payload_json" in final["result"]
    assert calls == []


def test_run_one_handler_exception_marks_failed(isolated_db, stub_handlers) -> None:
    stub_handlers("raise")
    row = _insert_approved("a1")
    asyncio.run(executor._run_one(row))
    final = queue.get("a1")
    assert final["status"] == "failed"
    assert "kaboom" in final["result"]


def test_run_one_executor_timeout_is_terminal_ambiguous(
    isolated_db, monkeypatch
) -> None:
    monkeypatch.setattr(executor, "HANDLER_TIMEOUT_SECS", 0.05)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    async def blocking_handler(_payload):
        def work():
            calls.append("started")
            started.set()
            release.wait(timeout=2)
            return {"ok": True, "result": "late effect"}

        return await asyncio.to_thread(work)

    monkeypatch.setitem(
        executor.HANDLERS, ActionType.PUSH_NOTIFICATION, blocking_handler
    )
    row = _insert_approved("a1")

    async def scenario() -> None:
        try:
            run = asyncio.create_task(executor._run_one(row))
            while not started.is_set():
                await asyncio.sleep(0.001)
            await run

            final = queue.get("a1")
            assert final["status"] == "ambiguous"
            assert final["retry_count"] == 0
            assert "outcome is unknown" in final["result"]
            assert isolated_db.get_db().execute(
                "SELECT status FROM action_idempotency_receipts WHERE action_id='a1'"
            ).fetchone() == ("ambiguous",)

            assert await executor.drain_once() == 0
            assert calls == ["started"]
        finally:
            release.set()
            await asyncio.sleep(0.01)

    asyncio.run(scenario())


def test_run_one_handler_raised_timeout_uses_typed_retry_policy(
    isolated_db, stub_handlers, monkeypatch
) -> None:
    monkeypatch.setattr(executor, "MAX_RETRIES", 1)
    stub_handlers("handler_timeout")
    row = _insert_approved("a1")

    asyncio.run(executor._run_one(row))

    after_first = queue.get("a1")
    assert after_first["status"] == "approved"
    assert after_first["retry_count"] == 1
    assert "provider deadline" in after_first["result"]


def test_run_one_unknown_type_marks_failed(isolated_db) -> None:
    row = _insert_approved("a1")
    row["type"] = "invented_type"
    asyncio.run(executor._run_one(row))
    final = queue.get("a1")
    assert final["status"] == "failed"
    assert "unknown type" in final["result"]


def test_run_one_missing_handler_marks_failed(isolated_db, monkeypatch) -> None:
    monkeypatch.setitem(
        executor.HANDLERS,
        ActionType.PUSH_NOTIFICATION,
        None,  # type: ignore[arg-type]
    )
    monkeypatch.delitem(executor.HANDLERS, ActionType.PUSH_NOTIFICATION)
    row = _insert_approved("a1")
    asyncio.run(executor._run_one(row))
    final = queue.get("a1")
    assert final["status"] == "failed"
    assert "no handler" in final["result"]


def test_drain_once_processes_only_approved_rows(isolated_db, stub_handlers) -> None:
    calls = stub_handlers("ok")
    _insert_approved("a1")
    queue.insert(Action(id="a2", type=ActionType.PUSH_NOTIFICATION, payload={}, rationale="r"))

    n = asyncio.run(executor.drain_once())

    assert n == 1
    assert len(calls) == 1
    assert queue.get("a1")["status"] == "executed"
    assert queue.get("a2")["status"] == "pending"


def test_drain_once_skips_already_executed(isolated_db, stub_handlers) -> None:
    calls = stub_handlers("ok")
    _insert_approved("a1")
    asyncio.run(executor._run_one(queue.get("a1")))
    assert queue.get("a1")["status"] == "executed"

    n = asyncio.run(executor.drain_once())
    assert n == 0
    assert len(calls) == 1


def test_drain_once_continues_after_individual_failure(isolated_db, stub_handlers) -> None:
    stub_handlers("raise")
    _insert_approved("a1", payload={"n": 1})
    _insert_approved("a2", payload={"n": 2})

    n = asyncio.run(executor.drain_once())

    assert n == 2
    assert queue.get("a1")["status"] == "failed"
    assert queue.get("a2")["status"] == "failed"
