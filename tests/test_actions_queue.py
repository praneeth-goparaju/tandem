from __future__ import annotations

import stat

import pytest


def test_pending_actions_table_exists(isolated_db) -> None:
    conn = isolated_db.get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_actions)").fetchall()}
    assert {
        "id", "type", "payload_json", "rationale", "source_finding_ids",
        "risk", "status", "approval_channel", "approval_signal",
        "payload_hash", "dedupe_key", "version", "approval_actor", "origin_agent",
        "origin_area", "approval_audience", "occurrence_key",
        "created_at", "decided_at", "executed_at", "result", "expires_at",
    }.issubset(cols)


def test_database_and_parent_permissions_are_private(isolated_db) -> None:
    path = isolated_db.MEMORY_DB_PATH
    isolated_db.get_db()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_database_rejects_preexisting_wal_symlink(isolated_db, tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("do not touch", encoding="utf-8")
    wal = isolated_db.MEMORY_DB_PATH.with_name(
        isolated_db.MEMORY_DB_PATH.name + "-wal"
    )
    wal.symlink_to(target)

    with pytest.raises(RuntimeError, match="not a regular file"):
        isolated_db.get_db()

    assert target.read_text(encoding="utf-8") == "do not touch"


def test_insert_and_get_by_id(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType, Risk

    a = Action(
        id="a1",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"title": "t", "message": "m"},
        rationale="why",
        risk=Risk.AUTO,
    )
    queue.insert(a)
    row = queue.get("a1")
    assert row is not None
    assert row["type"] == "push_notification"
    assert row["status"] == "pending"


def test_list_by_status_orders_by_created_at(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(id="a1", type=ActionType.PUSH_NOTIFICATION, payload={"n": 1}, rationale="r1"))
    queue.insert(Action(id="a2", type=ActionType.PUSH_NOTIFICATION, payload={"n": 2}, rationale="r2"))
    rows = queue.list_by_status("pending")
    assert [r["id"] for r in rows] == ["a1", "a2"]


def test_list_by_status_filters_by_origin_areas(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    for area in ("family", "health", "career"):
        queue.insert(Action(
            id=f"{area}-action",
            type=ActionType.PUSH_NOTIFICATION,
            payload={"message": area},
            rationale="test",
            origin_agent=area,
            origin_area=area,
        ))

    def ids(**filters) -> set[str]:
        return {row["id"] for row in queue.list_by_status("pending", **filters)}

    assert ids(origin_areas=["family", "health"]) == {"family-action", "health-action"}
    assert ids(origin_areas=["career"]) == {"career-action"}
    assert ids(
        origin_areas=["family"], origin_area="career", origin_agent="career"
    ) == {"family-action"}
    assert ids(origin_areas=[]) == set()
    assert ids(origin_area="health") == {"health-action"}
    assert ids() == {"family-action", "health-action", "career-action"}


def test_transition_status_is_guarded(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="a1",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"title": "Test", "message": "Review me"},
        rationale="r",
    ))
    assert queue.transition_status("a1", "pending", "approved", channel="slack_reply", signal="yes a1") is True
    assert queue.transition_status("a1", "pending", "approved") is False
    row = queue.get("a1")
    assert row["status"] == "approved"
    assert row["approval_channel"] == "slack_reply"
    assert row["approval_signal"] == "yes a1"
    assert row["decided_at"] is not None


def test_active_action_fingerprint_is_database_unique(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    first = Action(
        id="a1",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"message": "same"},
        rationale="first observer",
        origin_area="career",
    )
    duplicate = Action(
        id="a2",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"message": "same"},
        rationale="racing observer",
        origin_area="career",
    )
    queue.insert(first)

    with pytest.raises(queue.DuplicateActionError) as caught:
        queue.insert(duplicate)

    assert caught.value.existing_id == "a1"
    assert caught.value.existing_status == "pending"
    assert queue.get("a2") is None


def test_same_payload_in_different_areas_has_distinct_fingerprint(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    shared = {
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"message": "area-specific"},
        "rationale": "test",
    }
    queue.insert(Action(id="career-action", origin_area="career", **shared))
    queue.insert(Action(id="family-action", origin_area="family", **shared))

    assert queue.get("career-action")["dedupe_key"] != queue.get("family-action")["dedupe_key"]


def test_effect_terminal_action_retains_bounded_dedup_receipt(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    kwargs = {
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"message": "repeatable later"},
        "rationale": "test",
        "origin_area": "career",
    }
    queue.insert(Action(id="a1", **kwargs))
    assert queue.transition_status("a1", "pending", "approved")
    assert queue.transition_status("a1", "approved", "executed", result="done")

    with pytest.raises(queue.DuplicateActionError) as caught:
        queue.insert(Action(id="a2", **kwargs))

    assert caught.value.existing_id == "a1"
    assert caught.value.existing_status == "executed"
    assert queue.get("a2") is None

    receipt = isolated_db.get_db().execute(
        "SELECT status, valid_until FROM action_idempotency_receipts "
        "WHERE action_id='a1'"
    ).fetchone()
    assert receipt[0] == "executed"
    assert receipt[1] is not None


def test_occurrence_key_permits_intentional_repeat(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    kwargs = {
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"message": "same daily message"},
        "rationale": "test",
        "origin_area": "career",
    }
    queue.insert(Action(id="a1", occurrence_key="daily:2026-08-23", **kwargs))
    queue.transition_status("a1", "pending", "approved")
    queue.transition_status("a1", "approved", "executed", result="done")

    queue.insert(Action(id="a2", occurrence_key="daily:2026-08-24", **kwargs))

    assert queue.get("a2")["status"] == "pending"


def test_expired_terminal_receipt_is_pruned_atomically_on_insert(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    kwargs = {
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"message": "retry horizon elapsed"},
        "rationale": "test",
    }
    queue.insert(Action(id="a1", **kwargs))
    queue.transition_status("a1", "pending", "approved")
    queue.transition_status("a1", "approved", "executed", result="done")
    conn = isolated_db.get_db()
    conn.execute(
        "UPDATE action_idempotency_receipts "
        "SET valid_until='2000-01-01T00:00:00' WHERE action_id='a1'"
    )
    conn.commit()

    queue.insert(Action(id="a2", **kwargs))

    assert queue.get("a2")["status"] == "pending"
    assert conn.execute(
        "SELECT action_id, status FROM action_idempotency_receipts"
    ).fetchall() == [("a2", "pending")]


def test_rejected_action_releases_fingerprint_without_effect(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    kwargs = {
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"message": "safe to repropose"},
        "rationale": "test",
    }
    queue.insert(Action(id="a1", **kwargs))
    assert queue.transition_status("a1", "pending", "rejected")

    queue.insert(Action(id="a2", **kwargs))
    assert queue.get("a2")["status"] == "pending"


def test_database_rejects_new_action_without_fingerprint(isolated_db) -> None:
    import sqlite3

    conn = isolated_db.get_db()
    with pytest.raises(sqlite3.IntegrityError, match="dedupe_key is required"):
        conn.execute(
            "INSERT INTO pending_actions "
            "(id, type, payload_json, rationale, risk, status, created_at) "
            "VALUES ('missing-key', 'push_notification', '{}', 'r', "
            "'confirm', 'pending', '2026-08-23T00:00:00')"
        )
    conn.rollback()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("type", "send_draft"),
        ("payload_json", '{"changed": true}'),
        ("payload_hash", "changed"),
        ("dedupe_key", "changed"),
        ("version", 2),
        ("occurrence_key", "changed"),
        ("origin_agent", "other"),
        ("origin_area", "other"),
        ("approval_audience", "area:other"),
    ],
)
def test_action_provenance_columns_are_database_immutable(
    isolated_db, column: str, value: object
) -> None:
    import sqlite3

    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="a1",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"message": "original"},
        rationale="test",
        origin_agent="career",
        origin_area="career",
    ))
    conn = isolated_db.get_db()

    with pytest.raises(sqlite3.IntegrityError, match="provenance is immutable"):
        conn.execute(f"UPDATE pending_actions SET {column}=? WHERE id='a1'", (value,))
    conn.rollback()

    assert queue.get("a1")[column] != value


def test_approval_provenance_is_write_once_after_decision(isolated_db) -> None:
    import sqlite3

    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="a1",
        type=ActionType.CREATE_REMINDER,
        payload={
            "title": "Reviewable reminder",
            "due": "2026-09-01T09:00:00+02:00",
        },
        rationale="test",
    ))
    assert queue.transition_status(
        "a1",
        "pending",
        "approved",
        channel="slack_button",
        signal="approved by UOWNER",
        actor="UOWNER",
    )
    conn = isolated_db.get_db()

    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        conn.execute(
            "UPDATE pending_actions SET approval_actor='ATTACKER' WHERE id='a1'"
        )
    conn.rollback()

    row = queue.get("a1")
    assert row["approval_actor"] == "UOWNER"
    assert row["approval_channel"] == "slack_button"
    assert row["approval_signal"] == "approved by UOWNER"
