import json

import pytest


def test_intake_add_dedups_on_source_id(isolated_db):
    from src.memory import store

    first = store.intake_add("mc", "mc:captures:abc", "capture", "buy milk")
    assert first is not None
    dup = store.intake_add("mc", "mc:captures:abc", "capture", "buy milk again")
    assert dup is None
    count = isolated_db.get_db().execute("SELECT COUNT(*) FROM intake").fetchone()[0]
    assert count == 1


def test_intake_pending_oldest_first_pending_only(isolated_db):
    from src.memory import store

    a = store.intake_add("mc", "s1", "capture", "first")
    b = store.intake_add("mc", "s2", "capture", "second")
    c = store.intake_add("mc", "s3", "checkin", "third")
    assert store.intake_mark(b, "dismissed") is True
    pending = store.intake_pending()
    assert [r["id"] for r in pending] == [a, c]
    assert all(r["status"] == "pending" for r in pending)
    assert pending[0]["trust"] == "untrusted"
    assert pending[1]["kind"] == "checkin"


def test_intake_pending_can_leave_out_one_source(isolated_db):
    from src.memory import store

    capture = store.intake_add("mc", "s1", "capture", "buy milk")
    store.intake_add("public-discovery", "public-discovery:1", "web_candidate", "{}")
    assert [r["id"] for r in store.intake_pending(exclude_source="public-discovery")] == [capture]
    assert len(store.intake_pending()) == 2


def test_intake_pending_respects_limit(isolated_db):
    from src.memory import store

    for i in range(5):
        store.intake_add("mc", f"s{i}", "capture", f"c{i}")
    assert len(store.intake_pending(limit=3)) == 3


def test_intake_mark_sets_decided_at_and_promoted_to(isolated_db):
    from src.memory import store

    intake_id = store.intake_add("mc", "s1", "capture", "note")
    assert store.intake_mark(
        intake_id, "promoted", promoted_to="knowledge:k1"
    ) is True
    row = isolated_db.get_db().execute(
        "SELECT status, decided_at, promoted_to FROM intake WHERE id=?", (intake_id,)
    ).fetchone()
    assert row[0] == "promoted"
    assert row[1]
    assert row[2] == "knowledge:k1"


def test_intake_mark_rejects_unknown_status(isolated_db):
    from src.memory import store

    intake_id = store.intake_add("mc", "s1", "capture", "note")
    with pytest.raises(ValueError):
        store.intake_mark(intake_id, "bogus")


def test_intake_decision_is_compare_and_set(isolated_db):
    from src.memory import store

    intake_id = store.intake_add("mc", "cas", "capture", "note")
    assert store.intake_mark(intake_id, "dismissed") is True
    assert store.intake_mark(intake_id, "promoted", promoted_to="vault:x") is False
    assert isolated_db.get_db().execute(
        "SELECT status, promoted_to FROM intake WHERE id=?", (intake_id,)
    ).fetchone() == ("dismissed", None)


def test_promotion_claim_hides_row_and_blocks_dismissal(isolated_db):
    from src.memory import store

    intake_id = store.intake_add("mc", "claim", "capture", "note")
    assert store.intake_claim_promotion(intake_id, "action-1") is True
    assert store.intake_pending() == []
    assert store.intake_mark(intake_id, "dismissed") is False
    assert store.intake_claim_promotion(intake_id, "action-2") is False

    assert store.intake_finalize_promotion(
        intake_id, "action-1", "vault:Agent Notes/note.md"
    ) is True
    assert isolated_db.get_db().execute(
        "SELECT status, promoted_to, promotion_claim FROM intake WHERE id=?",
        (intake_id,),
    ).fetchone() == ("promoted", "vault:Agent Notes/note.md", None)


def test_promotion_claim_can_be_released_before_effect(isolated_db):
    from src.memory import store

    intake_id = store.intake_add("mc", "release", "capture", "note")
    assert store.intake_claim_promotion(intake_id, "action-1") is True
    assert store.intake_release_promotion(intake_id, "action-2") is False
    assert store.intake_release_promotion(intake_id, "action-1") is True
    assert [row["id"] for row in store.intake_pending()] == [intake_id]


def test_log_event_hashes_payload(isolated_db):
    from src.memory import store

    event_id = store.log_event(
        "assistant", "brief_published",
        subject="2026-07-07", trust="trusted", payload="hello world", detail="d",
    )
    assert isinstance(event_id, int)
    row = isolated_db.get_db().execute(
        "SELECT ts, actor, action, subject, trust, payload_hash, detail FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row[0]
    assert row[1:] == (
        "assistant", "brief_published", "2026-07-07", "trusted",
        isolated_db.content_hash("hello world"), "d",
    )


def test_log_event_without_payload_leaves_hash_null(isolated_db):
    from src.memory import store

    event_id = store.log_event("system", "boot")
    row = isolated_db.get_db().execute(
        "SELECT payload_hash, subject, trust, detail FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row == (None, None, None, "")
