from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def test_sweep_expired_transitions_only_past_pending(isolated_db) -> None:
    from src.brain.actions import expiry, queue
    from src.brain.actions.types import Action, ActionType

    past = _iso(datetime.now(UTC) - timedelta(hours=1))
    future = _iso(datetime.now(UTC) + timedelta(hours=1))

    queue.insert(Action(id="old", type=ActionType.PUSH_NOTIFICATION, payload={"n": "old"}, rationale="r", expires_at=past))
    queue.insert(Action(id="new", type=ActionType.PUSH_NOTIFICATION, payload={"n": "new"}, rationale="r", expires_at=future))
    queue.insert(Action(id="none", type=ActionType.PUSH_NOTIFICATION, payload={"n": "none"}, rationale="r"))

    n = expiry.sweep_expired()
    assert n == 1
    assert queue.get("old")["status"] == "expired"
    assert queue.get("new")["status"] == "pending"
    assert queue.get("none")["status"] == "pending"


def test_sweep_expired_skips_already_decided_rows(isolated_db) -> None:
    from src.brain.actions import expiry, queue
    from src.brain.actions.types import Action, ActionType

    past = _iso(datetime.now(UTC) - timedelta(hours=1))
    queue.insert(Action(id="approved-old", type=ActionType.PUSH_NOTIFICATION, payload={}, rationale="r", expires_at=past))
    queue.transition_status("approved-old", "pending", "approved")

    n = expiry.sweep_expired()
    assert n == 0
    assert queue.get("approved-old")["status"] == "approved"


def test_sweeper_expires_malformed_legacy_timestamp(isolated_db) -> None:
    from src.brain.actions import expiry, queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="malformed-old",
        type=ActionType.PUSH_NOTIFICATION,
        payload={},
        rationale="r",
        expires_at="not-a-timestamp",
    ))

    assert expiry.sweep_expired() == 1
    assert queue.get("malformed-old")["status"] == "expired"
