from __future__ import annotations

import json

from src.brain.actions.types import (
    HUMAN_APPROVAL_CHANNELS,
    OWN_DEVICE_AUTO_TYPES,
    Action,
    ActionType,
    Risk,
    Status,
)


def test_enums_have_expected_values() -> None:
    assert {t.value for t in ActionType} >= {"send_draft", "push_notification"}
    assert {r.value for r in Risk} == {"auto", "confirm", "manual"}
    assert {s.value for s in Status} == {
        "pending", "approved", "executing", "rejected", "expired", "executed",
        "partial", "ambiguous", "failed",
    }
    assert HUMAN_APPROVAL_CHANNELS == {"slack_reply", "slack_button", "signal_reply"}


def test_push_notification_is_not_an_own_device_type() -> None:
    assert ActionType.PUSH_NOTIFICATION not in OWN_DEVICE_AUTO_TYPES


def test_a_stuck_claim_is_swept_only_well_after_the_handler_timeout() -> None:
    from src.brain.actions.executor import HANDLER_TIMEOUT_SECS
    from src.brain.actions.expiry import STUCK_EXECUTING_CEILING_SECS

    assert STUCK_EXECUTING_CEILING_SECS >= 5 * HANDLER_TIMEOUT_SECS


def test_action_roundtrip_to_row_dict() -> None:
    a = Action(
        id="abc123",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"title": "x", "message": "y"},
        rationale="urgent finding deserves an immediate ping",
        source_finding_ids=["f1", "f2"],
        risk=Risk.AUTO,
    )
    row = a.to_row()
    assert row["type"] == "push_notification"
    assert row["risk"] == "auto"
    assert row["status"] == "pending"
    assert row["source_finding_ids"] == "f1,f2"
    assert json.loads(row["payload_json"]) == {"title": "x", "message": "y"}
    assert row["created_at"]


def test_occurrence_key_changes_only_the_logical_occurrence_fingerprint() -> None:
    shared = {
        "id": "unused",
        "type": ActionType.PUSH_NOTIFICATION,
        "payload": {"title": "same", "message": "same"},
        "rationale": "test",
    }
    first = Action(**shared, occurrence_key="daily:2026-08-23").to_row()
    retry = Action(**shared, occurrence_key="daily:2026-08-23").to_row()
    next_day = Action(**shared, occurrence_key="daily:2026-08-24").to_row()

    assert first["payload_hash"] == next_day["payload_hash"]
    assert first["dedupe_key"] == retry["dedupe_key"]
    assert first["dedupe_key"] != next_day["dedupe_key"]
