from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def due_reminder(monkeypatch, tmp_path):
    from src.brain.occasions import store
    from src.brain.occasions.models import (
        OccasionFlag,
        PeopleFile,
        Person,
        PersonOccasions,
    )
    from src.orchestrator import config

    root = tmp_path / "occasions"
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    monkeypatch.setattr(config, "OCCASIONS_DIR", root)
    monkeypatch.setattr(config, "OCCASIONS_PEOPLE", root / "people.yaml")
    monkeypatch.setattr(config, "OCCASIONS_FESTIVALS", root / "festivals.yaml")
    monkeypatch.setattr(config, "OCCASIONS_VOICE", root / "voice.yaml")

    tomorrow = store.local_date() + timedelta(days=1)
    store.save_people(PeopleFile(people=[Person(
        id="ravi",
        name="Ravi",
        occasions=PersonOccasions(birthday=OccasionFlag(
            enabled=True,
            date=f"--{tomorrow.month:02d}-{tomorrow.day:02d}",
        )),
    )]))
    return store, tomorrow


def _push(year: int, *, untrusted_input: bool):
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.notifications import push_notify

    caller = ToolCaller(
        agent="humans",
        area="humans",
        allowed_action_types=frozenset({"push_notification"}),
        untrusted_input=untrusted_input,
    )
    handler = getattr(push_notify, "handler", push_notify)
    with caller_scope(caller):
        return asyncio.run(handler({
            "title": "Ravi's birthday",
            "message": "Tomorrow",
            "thread_detail": "Suggested wording",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": year,
        }))


def _notifier() -> AsyncMock:
    notifier = AsyncMock()
    notifier.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "111.222", "group_id": "C_HUMANS"},
        {"status": "sent", "id": "111.333", "group_id": "C_HUMANS"},
    ])
    notifier.persona_for = lambda *_args, **_kwargs: {"name": "Humans"}
    return notifier


def _approval_notifier() -> AsyncMock:
    from src.brain.actions import queue

    async def send(_text, **kwargs):
        action_id = kwargs["proposal_action_ids"][0]
        row = queue.get(action_id)
        queue.record_review_delivery(
            action_id, row["payload_hash"], "humans", "C_HUMANS", "100.200"
        )
        return {"status": "sent", "id": "100.200", "group_id": "C_HUMANS"}

    notifier = AsyncMock()
    notifier.send = AsyncMock(side_effect=send)
    return notifier


def test_auto_reminder_is_stamped_before_push_returns(
    isolated_db, due_reminder
) -> None:
    from src.brain.actions import queue

    store, tomorrow = due_reminder
    notifier = _notifier()
    with patch(
        "src.brain.actions.handlers.push_notification.notifier",
        return_value=notifier,
    ):
        result = _push(tomorrow.year, untrusted_input=False)

    assert "executed" in result["content"][0]["text"].lower()
    assert store.already_reminded("ravi", "birthday", tomorrow.year)
    assert store.people_by_id()["ravi"].last_reminded[
        f"birthday:{tomorrow.year}"
    ] == "111.222"
    assert queue.list_by_status("executed")[0]["status"] == "executed"


def test_tainted_reminder_waits_for_review_and_execution_before_stamping(
    isolated_db, due_reminder
) -> None:
    from src.brain.actions import approvals, executor, queue
    from src.brain.actions.approval_review import build_approval_review

    store, tomorrow = due_reminder
    notifier = _notifier()
    approval_notifier = _approval_notifier()
    with (
        patch(
            "src.brain.actions.handlers.push_notification.notifier",
            return_value=notifier,
        ),
        patch(
            "src.brain.mcp.tools.actions.notifier",
            return_value=approval_notifier,
        ),
    ):
        result = _push(tomorrow.year, untrusted_input=True)

        assert "queued" in result["content"][0]["text"].lower()
        assert "approval review delivered" in result["content"][0]["text"].lower()
        row = queue.list_by_status("pending")[0]
        assert not store.already_reminded("ravi", "birthday", tomorrow.year)
        notifier.send.assert_not_awaited()
        assert approval_notifier.send.await_args.kwargs["proposal_action_ids"] == [
            row["id"]
        ]

        review = build_approval_review(row)
        assert review.approvable is True
        rendered = "\n".join(review.summary_lines)
        assert "Occasion person: ravi" in rendered
        assert "Occasion: birthday" in rendered
        assert f"Occasion year: {tomorrow.year}" in rendered
        assert "suppress this exact occasion reminder" in rendered

        assert approvals.handle_reply(
            f"approve {row['id']}",
            area="humans",
            actor="UOWNER",
            owner=True,
        )
        assert queue.get(row["id"])["status"] == "approved"
        assert not store.already_reminded("ravi", "birthday", tomorrow.year)

        assert asyncio.run(executor.drain_once()) == 1

    assert queue.get(row["id"])["status"] == "executed"
    assert store.already_reminded("ravi", "birthday", tomorrow.year)
    assert notifier.send.await_count == 2


def test_action_creation_date_uses_configured_timezone_at_utc_midnight(
    isolated_db, monkeypatch
) -> None:
    from src.brain.actions.handlers.push_notification import execute
    from src.brain.occasions import store
    from src.orchestrator import config

    monkeypatch.setattr(config, "TIMEZONE", "Europe/Berlin")
    assert store.local_date(datetime(2026, 8, 24, 22, 30)) == date(2026, 8, 25)

    with patch(
        "src.brain.occasions.store.reminder_identity_status",
        return_value="already_reminded",
    ) as status:
        result = asyncio.run(execute({
            "message": "Tomorrow",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": 2026,
            "_action_id": "abc123def456",
            "_origin_agent": "humans",
            "_origin_area": "humans",
            "_action_created_at": "2026-08-24T22:30:00",
        }))

    assert result["ok"] is True
    assert status.call_args.kwargs["created_on"] == date(2026, 8, 25)


def test_scan_uses_the_same_configured_local_date(due_reminder, monkeypatch) -> None:
    from src.brain.mcp.tools.occasions import occasions_scan

    store, tomorrow = due_reminder
    today = tomorrow - timedelta(days=1)
    monkeypatch.setattr(store, "local_date", lambda: today)

    result = asyncio.run(occasions_scan({"days": 1}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["today"] == today.isoformat()
    assert payload["occasions"][0]["days_out"] == 1


def test_model_authored_legacy_route_cannot_authorize_reminder_stamp(
    isolated_db, due_reminder
) -> None:
    from src.brain.actions.handlers.push_notification import execute

    store, tomorrow = due_reminder
    with patch("src.brain.actions.handlers.push_notification.notifier") as notifier:
        result = asyncio.run(execute({
            "message": "Tomorrow",
            "occasion_person_id": "ravi",
            "occasion": "birthday",
            "occasion_year": tomorrow.year,
            "agent": "humans",
            "area": "humans",
            "_action_id": "abc123def456",
            "_action_created_at": datetime.now().isoformat(),
        }))

    assert result == {"ok": False, "result": "invalid occasion reminder provenance"}
    notifier.assert_not_called()
    assert not store.already_reminded("ravi", "birthday", tomorrow.year)
