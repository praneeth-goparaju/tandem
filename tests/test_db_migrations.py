from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.db as dbmod
from src.db import LATEST_SCHEMA_VERSION, MIGRATIONS, _apply_migration, _init_schema


def _legacy_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE notifications (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            priority TEXT DEFAULT 'default',
            status TEXT NOT NULL,
            http_status INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE pending_actions (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            source_finding_ids TEXT DEFAULT '',
            risk TEXT NOT NULL DEFAULT 'confirm',
            status TEXT NOT NULL DEFAULT 'pending',
            approval_channel TEXT,
            approval_signal TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            executed_at TEXT,
            result TEXT,
            expires_at TEXT
        );
        CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT,
            findings_count INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0,
            error TEXT
        );
        """
    )
    return conn


def test_legacy_schema_upgrades_and_records_every_version() -> None:
    conn = _legacy_connection()
    _init_schema(conn)

    pending_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_actions)")
    }
    assert {
        "retry_count",
        "payload_hash",
        "dedupe_key",
        "version",
        "approval_actor",
        "origin_agent",
        "origin_area",
        "approval_audience",
        "occurrence_key",
        "claimed_at",
    }.issubset(pending_columns)
    intake_columns = {row[1] for row in conn.execute("PRAGMA table_info(intake)")}
    assert "promotion_claim" in intake_columns
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='action_note_task_links'"
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='action_idempotency_receipts'"
    ).fetchone()

    applied = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert applied == [(version, name) for version, name, _fn in MIGRATIONS]
    assert applied[-1][0] == LATEST_SCHEMA_VERSION

    _init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
        len(MIGRATIONS),
    )


def test_failed_migration_rolls_back_schema_and_ledger() -> None:
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)

    def broken(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE should_rollback (id INTEGER)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _apply_migration(conn, 99, "broken-test", broken)

    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=99"
    ).fetchone() is None


def test_migration_version_cannot_be_reused_under_a_new_name() -> None:
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    with pytest.raises(RuntimeError, match="was recorded as"):
        _apply_migration(conn, 1, "renamed", lambda _conn: None)


def test_future_schema_version_is_rejected_before_startup() -> None:
    conn = _legacy_connection()
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    future = LATEST_SCHEMA_VERSION + 1
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?, 'future-release', '2026-08-23T00:00:00')",
        (future,),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        _init_schema(conn)

    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
        future,
    )
    assert conn.in_transaction is False


def test_schema_initialization_serializes_concurrent_migrators(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "concurrent.db"
    entered = threading.Event()
    barrier = threading.Barrier(2)
    calls: list[int] = []

    def slow_migration(conn: sqlite3.Connection) -> None:
        calls.append(1)
        entered.set()
        time.sleep(0.15)
        conn.execute("CREATE TABLE serialized_once (id INTEGER)")

    monkeypatch.setattr(dbmod, "MIGRATIONS", ((1, "serialized-once", slow_migration),))
    monkeypatch.setattr(dbmod, "LATEST_SCHEMA_VERSION", 1)

    def initialize() -> None:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.execute("PRAGMA busy_timeout=2000")
        barrier.wait(timeout=2)
        dbmod._init_schema(conn)
        conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize) for _ in range(2)]
        assert entered.wait(timeout=2)
        for future in futures:
            future.result(timeout=3)

    assert calls == [1]
    verify = sqlite3.connect(db_path)
    assert verify.execute(
        "SELECT version, name FROM schema_migrations"
    ).fetchall() == [(1, "serialized-once")]
    verify.close()


def test_legacy_active_fingerprints_are_backfilled_and_duplicates_terminalized() -> None:
    from src.brain.actions.types import Action, ActionType

    conn = _legacy_connection()
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    payload = '{"message": "same legacy action"}'
    conn.executemany(
        "INSERT INTO pending_actions "
        "(id, type, payload_json, rationale, risk, status, created_at) "
        "VALUES (?, 'push_notification', ?, 'legacy', 'confirm', ?, ?)",
        (
            ("pending-old", payload, "pending", "2026-08-20T08:00:00"),
            ("approved-old", payload, "approved", "2026-08-20T09:00:00"),
            ("executing-old", payload, "executing", "2026-08-20T10:00:00"),
        ),
    )
    conn.commit()

    _init_schema(conn)

    rows = conn.execute(
        "SELECT id, status, payload_hash, dedupe_key, result "
        "FROM pending_actions ORDER BY id"
    ).fetchall()
    by_id = {row[0]: row[1:] for row in rows}
    assert by_id["executing-old"][0] == "executing"
    assert by_id["approved-old"][0] == "failed"
    assert by_id["pending-old"][0] == "failed"
    assert "superseded by executing-old" in by_id["approved-old"][3]
    assert {len(row[2]) for row in rows} == {64}
    assert {len(row[3]) for row in rows} == {64}
    assert len({row[3] for row in rows}) == 1
    expected = Action(
        id="new-shape",
        type=ActionType.PUSH_NOTIFICATION,
        payload={"message": "same legacy action"},
        rationale="test",
    ).to_row()
    assert rows[0][2] == expected["payload_hash"]
    assert rows[0][3] == expected["dedupe_key"]

    receipt = conn.execute(
        "SELECT action_id, status, valid_until FROM action_idempotency_receipts"
    ).fetchone()
    assert receipt == ("executing-old", "executing", None)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_pending_actions_active_dedupe'"
    ).fetchone()


def test_legacy_actorless_approvals_reopen_safely_and_mail_drafts_retire(
    monkeypatch,
) -> None:
    from src.brain.actions import executor, queue
    from src.brain.actions.types import ActionType

    conn = _legacy_connection()
    now = dbmod.utcnow()
    conn.execute("ALTER TABLE pending_actions ADD COLUMN retry_count INTEGER DEFAULT 0")
    conn.executemany(
        "INSERT INTO pending_actions "
        "(id, type, payload_json, rationale, risk, status, approval_channel, "
        "approval_signal, created_at, decided_at, executed_at, result) "
        "VALUES (?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                "legacy-confirm",
                "promote_note",
                '{"body": "reviewed", "title": "Legacy"}',
                "confirm",
                "approved",
                "signal_reply",
                "yes legacy-confirm",
                now,
                now,
                None,
                None,
            ),
            (
                "legacy-retried",
                "update_contact",
                '{"href": "/card.vcf", "note_append": "reviewed"}',
                "confirm",
                "approved",
                "signal_reply",
                "yes legacy-retried",
                now,
                now,
                None,
                "retry 1/2",
            ),
            (
                "legacy-auto",
                "push_notification",
                '{"message": "still approved"}',
                "auto",
                "approved",
                "auto",
                None,
                now,
                now,
                None,
                None,
            ),
            (
                "archive-legacy",
                "mail_archive",
                '{"provider": "icloud", "uid": "42"}',
                "confirm",
                "approved",
                "signal_reply",
                "yes archive-legacy",
                now,
                now,
                None,
                None,
            ),
            (
                "archive-reviewable",
                "mail_archive",
                json.dumps({
                    "provider": "icloud", "mailbox": "INBOX", "uid": "42",
                    "uidvalidity": "7", "message_id": "<m@example.com>",
                    "message_from": "sender@example.com",
                    "message_subject": "Subject",
                    "message_header_sha256": "a" * 64,
                }),
                "confirm",
                "approved",
                "signal_reply",
                "yes archive-reviewable",
                now,
                now,
                None,
                None,
            ),
            (
                "archive-executing",
                "mail_archive",
                '{"provider": "icloud", "uid": "43"}',
                "confirm",
                "executing",
                "signal_reply",
                "yes archive-executing",
                now,
                now,
                None,
                None,
            ),
            (
                "archive-noncanonical",
                "mail_archive",
                json.dumps({
                    "provider": "icloud", "mailbox": "INBOX", "uid": "42",
                    "uidvalidity": "07", "message_id": "<m@example.com>",
                    "message_from": "sender@example.com",
                    "message_subject": "Subject",
                    "message_header_sha256": "a" * 64,
                }),
                "confirm",
                "pending",
                None,
                None,
                now,
                None,
                None,
                None,
            ),
            (
                "mail-pending",
                "mail_draft",
                '{"to": "a@example.test", "body": "pending"}',
                "auto",
                "pending",
                None,
                None,
                now,
                None,
                None,
                None,
            ),
            (
                "mail-approved",
                "mail_draft",
                '{"to": "a@example.test", "body": "approved"}',
                "auto",
                "approved",
                "auto",
                None,
                now,
                now,
                None,
                None,
            ),
            (
                "mail-executing",
                "mail_draft",
                '{"to": "a@example.test", "body": "executing"}',
                "auto",
                "executing",
                "auto",
                None,
                now,
                now,
                None,
                None,
            ),
            (
                "mail-executed",
                "mail_draft",
                '{"to": "a@example.test", "body": "done"}',
                "auto",
                "executed",
                "auto",
                None,
                now,
                now,
                now,
                "draft saved",
            ),
        ),
    )
    conn.execute(
        "UPDATE pending_actions SET retry_count=1 WHERE id='legacy-retried'"
    )
    conn.commit()

    _init_schema(conn)

    reopened = conn.execute(
        "SELECT status, approval_channel, approval_signal, approval_actor, "
        "decided_at, result, expires_at FROM pending_actions "
        "WHERE id='legacy-confirm'"
    ).fetchone()
    assert reopened[:6] == ("pending", None, None, None, None, None)
    assert reopened[6] > now

    retried = conn.execute(
        "SELECT status, retry_count, result FROM pending_actions "
        "WHERE id='legacy-retried'"
    ).fetchone()
    assert retried[:2] == ("ambiguous", 1)
    assert "outcome unknown" in retried[2]

    assert conn.execute(
        "SELECT status, approval_channel, approval_actor FROM pending_actions "
        "WHERE id='legacy-auto'"
    ).fetchone() == ("approved", "auto", None)

    mail_rows = dict(
        conn.execute(
            "SELECT id, status FROM pending_actions WHERE type='mail_draft'"
        ).fetchall()
    )
    assert mail_rows == {
        "mail-pending": "expired",
        "mail-approved": "expired",
        "mail-executing": "ambiguous",
        "mail-executed": "executed",
    }
    assert "review Drafts manually" in conn.execute(
        "SELECT result FROM pending_actions WHERE id='mail-executing'"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT result FROM pending_actions WHERE id='mail-executed'"
    ).fetchone() == ("draft saved",)

    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id='archive-legacy'"
    ).fetchone() == ("expired",)
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id='archive-noncanonical'"
    ).fetchone() == ("expired",)
    assert conn.execute(
        "SELECT status, approval_actor FROM pending_actions "
        "WHERE id='archive-reviewable'"
    ).fetchone() == ("pending", None)
    ambiguous_archive = conn.execute(
        "SELECT status, result FROM pending_actions WHERE id='archive-executing'"
    ).fetchone()
    assert ambiguous_archive[0] == "ambiguous"
    assert "reconcile manually" in ambiguous_archive[1]

    monkeypatch.setattr(queue, "get_db", lambda: conn)
    calls: list[dict] = []

    async def succeed(payload: dict) -> dict:
        calls.append(payload)
        return {"ok": True, "result": "reapproved effect"}

    monkeypatch.setitem(executor.HANDLERS, ActionType.PROMOTE_NOTE, succeed)
    assert queue.transition_status(
        "legacy-confirm",
        "pending",
        "approved",
        channel="slack_reply",
        signal="yes legacy-confirm",
        actor="U-owner",
    )
    asyncio.run(executor._run_one(queue.get("legacy-confirm")))

    assert calls and calls[0]["body"] == "reviewed"
    assert conn.execute(
        "SELECT status, approval_actor, result FROM pending_actions "
        "WHERE id='legacy-confirm'"
    ).fetchone() == ("executed", "U-owner", "reapproved effect")


def test_migration_16_creates_vault_edits_and_from_outside(isolated_db) -> None:
    conn = isolated_db.get_db()
    assert conn.execute(
        "SELECT name FROM schema_migrations WHERE version=16"
    ).fetchone() == ("alfred-stage-1",)

    # PRAGMA table_info rows are (cid, name, type, notnull, default, pk).
    edits = {row[1]: row for row in conn.execute("PRAGMA table_info(vault_edits)")}
    assert list(edits) == [
        "id", "ts", "agent", "run_label", "tool", "path", "sha_before", "sha_after",
    ]
    assert [edits[name][3] for name in ("ts", "agent", "run_label", "tool", "path")] == [1] * 5
    assert [edits[name][3] for name in ("sha_before", "sha_after")] == [0, 0]
    findings = {row[1]: row for row in conn.execute("PRAGMA table_info(findings)")}
    assert findings["from_outside"][2:5] == ("INTEGER", 1, "0")


def test_migration_16_upgrades_a_database_made_before_it(isolated_db) -> None:
    conn = isolated_db.get_db()
    conn.execute("DROP TABLE vault_edits")
    conn.execute("ALTER TABLE findings DROP COLUMN from_outside")
    conn.execute("DELETE FROM schema_migrations WHERE version=16")
    conn.execute(
        "INSERT INTO findings (id, agent, category, title, created_at) "
        "VALUES ('old-finding', 'family', 'note', 'stored before the column', 't')"
    )
    conn.commit()

    isolated_db._apply_migrations(conn)

    assert conn.execute(
        "SELECT from_outside FROM findings WHERE id='old-finding'"
    ).fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM vault_edits").fetchone() == (0,)
    isolated_db._apply_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=16").fetchone() == (1,)


def test_migration_17_creates_media_and_follow_ups(isolated_db):
    conn = isolated_db.get_db()
    media_cols = [row[1] for row in conn.execute("PRAGMA table_info(media)")]
    follow_cols = [row[1] for row in conn.execute("PRAGMA table_info(follow_ups)")]
    assert media_cols == ["id", "ts", "agent", "run_label", "area", "place", "kind", "path",
                          "bytes", "sha256", "sent_ts", "sent_channel", "deleted_at"]
    assert follow_cols[:11] == ["id", "source", "ref", "asked_by", "place", "summary", "status",
                                "first_seen", "last_raised", "research", "dedupe_key"]
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=17").fetchone()
