from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import local

from src.brain.sqlite_security import secure_sqlite_files, secure_sqlite_path
from src.orchestrator.config import MEMORY_DB_PATH

_local = local()

ACTION_RECEIPT_RETENTION_DAYS = 7


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def short_id() -> str:
    return uuid.uuid4().hex[:12]


def content_hash(text: str) -> str:
    """SHA-256 hash of normalized text, truncated to 16 chars."""
    return hashlib.sha256(text.lower().strip().encode()).hexdigest()[:16]


def get_db() -> sqlite3.Connection:
    """Get a thread-local database connection. Creates tables on first call per thread."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            _local.conn = None

    db_path = secure_sqlite_path(MEMORY_DB_PATH)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        os.chmod(db_path, 0o600, follow_symlinks=False)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(conn)
        secure_sqlite_files(db_path)
    except BaseException:
        conn.close()
        raise
    _local.conn = conn
    return conn


def _migrate_add_column(conn: sqlite3.Connection, table: str, col_decl: str) -> None:
    col_name = col_decl.split()[0]
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_decl}")


MigrationFn = Callable[[sqlite3.Connection], None]


def _migration_notifications_routing(conn: sqlite3.Connection) -> None:
    for decl in (
        "finding_id TEXT",
        "signal_timestamp INTEGER",
        "area TEXT",
        "group_id TEXT",
    ):
        _migrate_add_column(conn, "notifications", decl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_signal_ts "
        "ON notifications(signal_timestamp)"
    )


def _migration_action_retries(conn: sqlite3.Connection) -> None:
    _migrate_add_column(conn, "pending_actions", "retry_count INTEGER DEFAULT 0")


def _create_action_approval_write_once_trigger(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS pending_actions_approval_write_once "
        "BEFORE UPDATE OF approval_actor, approval_channel, approval_signal "
        "ON pending_actions FOR EACH ROW WHEN ("
        "NEW.approval_actor IS NOT OLD.approval_actor OR "
        "NEW.approval_channel IS NOT OLD.approval_channel OR "
        "NEW.approval_signal IS NOT OLD.approval_signal"
        ") AND NOT (OLD.status = 'pending' AND "
        "NEW.status IN ('approved', 'rejected')) "
        "BEGIN SELECT RAISE(ABORT, 'action approval provenance is write-once'); END"
    )


def _migration_agent_run_telemetry(conn: sqlite3.Connection) -> None:
    for decl in (
        "duration_ms INTEGER DEFAULT 0",
        "cost_usd REAL DEFAULT 0",
        "input_tokens INTEGER DEFAULT 0",
        "output_tokens INTEGER DEFAULT 0",
        "cache_read_tokens INTEGER DEFAULT 0",
        "cache_creation_tokens INTEGER DEFAULT 0",
        "provider TEXT DEFAULT 'pi'",
        "model TEXT DEFAULT ''",
    ):
        _migrate_add_column(conn, "agent_runs", decl)


def _migration_action_provenance(conn: sqlite3.Connection) -> None:
    for decl in (
        "payload_hash TEXT",
        "dedupe_key TEXT",
        "version INTEGER NOT NULL DEFAULT 1",
        "approval_actor TEXT",
        "origin_agent TEXT",
        "origin_area TEXT",
        "approval_audience TEXT NOT NULL DEFAULT 'owner'",
    ):
        _migrate_add_column(conn, "pending_actions", decl)

    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS pending_actions_dedupe_key_required "
        "BEFORE INSERT ON pending_actions FOR EACH ROW "
        "WHEN NEW.dedupe_key IS NULL OR length(NEW.dedupe_key) != 64 "
        "BEGIN SELECT RAISE(ABORT, 'pending action dedupe_key is required'); END"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_actions_active_dedupe "
        "ON pending_actions(dedupe_key) "
        "WHERE dedupe_key IS NOT NULL "
        "AND status IN ('pending', 'approved', 'executing')"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS pending_actions_provenance_immutable "
        "BEFORE UPDATE OF type, payload_json, payload_hash, dedupe_key, version, "
        "origin_agent, origin_area, approval_audience ON pending_actions "
        "FOR EACH ROW WHEN "
        "NEW.type IS NOT OLD.type OR "
        "NEW.payload_json IS NOT OLD.payload_json OR "
        "NEW.payload_hash IS NOT OLD.payload_hash OR "
        "NEW.dedupe_key IS NOT OLD.dedupe_key OR "
        "NEW.version IS NOT OLD.version OR "
        "NEW.origin_agent IS NOT OLD.origin_agent OR "
        "NEW.origin_area IS NOT OLD.origin_area OR "
        "NEW.approval_audience IS NOT OLD.approval_audience "
        "BEGIN SELECT RAISE(ABORT, 'pending action provenance is immutable'); END"
    )
    _create_action_approval_write_once_trigger(conn)
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS pending_actions_approval_empty_on_insert "
        "BEFORE INSERT ON pending_actions FOR EACH ROW WHEN "
        "NEW.approval_actor IS NOT NULL OR "
        "NEW.approval_channel IS NOT NULL OR "
        "NEW.approval_signal IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'action approval provenance must be empty on insert'); END"
    )


def _migration_action_note_task_links(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_note_task_links (
            action_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (action_id, task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_action_note_task_id
            ON action_note_task_links(task_id)
        """
    )


def _migration_action_reliability_ledger(conn: sqlite3.Connection) -> None:
    import json

    from src.brain.actions.fingerprints import dedupe_key, is_sha256_hex, payload_hash

    conn.execute("DROP TRIGGER IF EXISTS pending_actions_provenance_immutable")
    conn.execute("DROP INDEX IF EXISTS idx_pending_actions_active_dedupe")
    _migrate_add_column(conn, "pending_actions", "occurrence_key TEXT")

    rows = conn.execute(
        "SELECT id, type, payload_json, version, origin_area, occurrence_key, "
        "payload_hash, dedupe_key FROM pending_actions ORDER BY id"
    ).fetchall()
    for (
        action_id,
        action_type,
        payload_json,
        version,
        origin_area,
        occurrence_key,
        existing_payload_hash,
        existing_dedupe_key,
    ) in rows:
        raw_payload = payload_json or ""
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            payload = {"_invalid_legacy_payload_json": raw_payload}
        conn.execute(
            "UPDATE pending_actions SET payload_hash=?, dedupe_key=? WHERE id=?",
            (
                (
                    existing_payload_hash
                    if is_sha256_hex(existing_payload_hash)
                    else payload_hash(raw_payload)
                ),
                existing_dedupe_key if is_sha256_hex(existing_dedupe_key) else dedupe_key(
                    action_type=action_type,
                    payload=payload,
                    origin_area=origin_area,
                    version=int(version or 1),
                    occurrence_key=occurrence_key,
                ),
                action_id,
            ),
        )

    duplicate_keys = conn.execute(
        "SELECT dedupe_key FROM pending_actions "
        "WHERE status IN ('pending', 'approved', 'executing') "
        "GROUP BY dedupe_key HAVING COUNT(*) > 1 ORDER BY dedupe_key"
    ).fetchall()
    for (key,) in duplicate_keys:
        group = conn.execute(
            "SELECT id FROM pending_actions WHERE dedupe_key=? "
            "AND status IN ('pending', 'approved', 'executing') "
            "ORDER BY CASE status WHEN 'executing' THEN 0 WHEN 'approved' THEN 1 "
            "ELSE 2 END, created_at, id",
            (key,),
        ).fetchall()
        survivor = group[0][0]
        for (duplicate_id,) in group[1:]:
            conn.execute(
                "UPDATE pending_actions SET status='failed', executed_at=?, result=? "
                "WHERE id=?",
                (
                    utcnow(),
                    f"migration: duplicate active action superseded by {survivor}",
                    duplicate_id,
                ),
            )

    conn.execute(
        "CREATE UNIQUE INDEX idx_pending_actions_active_dedupe "
        "ON pending_actions(dedupe_key) "
        "WHERE status IN ('pending', 'approved', 'executing')"
    )
    conn.execute(
        "CREATE TRIGGER pending_actions_provenance_immutable "
        "BEFORE UPDATE OF type, payload_json, payload_hash, dedupe_key, version, "
        "occurrence_key, origin_agent, origin_area, approval_audience "
        "ON pending_actions FOR EACH ROW WHEN "
        "NEW.type IS NOT OLD.type OR "
        "NEW.payload_json IS NOT OLD.payload_json OR "
        "NEW.payload_hash IS NOT OLD.payload_hash OR "
        "NEW.dedupe_key IS NOT OLD.dedupe_key OR "
        "NEW.version IS NOT OLD.version OR "
        "NEW.occurrence_key IS NOT OLD.occurrence_key OR "
        "NEW.origin_agent IS NOT OLD.origin_agent OR "
        "NEW.origin_area IS NOT OLD.origin_area OR "
        "NEW.approval_audience IS NOT OLD.approval_audience "
        "BEGIN SELECT RAISE(ABORT, 'pending action provenance is immutable'); END"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_idempotency_receipts ("
        "dedupe_key TEXT PRIMARY KEY, "
        "action_id TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "valid_until TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_receipts_valid_until "
        "ON action_idempotency_receipts(valid_until)"
    )
    conn.execute("DELETE FROM action_idempotency_receipts")

    receipt_query = (
        "SELECT dedupe_key, id, status, created_at, "  # noqa: S608
        "CASE WHEN status IN ('pending', 'approved', 'executing') THEN NULL "
        "ELSE strftime('%Y-%m-%dT%H:%M:%S', "
        "COALESCE(executed_at, decided_at, created_at), "
        f"'+{ACTION_RECEIPT_RETENTION_DAYS} days') END "
        "FROM pending_actions WHERE dedupe_key IS NOT NULL AND ("
        "status IN ('pending', 'approved', 'executing') OR ("
        "status IN ('executed', 'partial', 'ambiguous', 'failed') AND "
        f"datetime(COALESCE(executed_at, decided_at, created_at)) > "
        f"datetime('now', '-{ACTION_RECEIPT_RETENTION_DAYS} days'))) "
        "ORDER BY CASE WHEN status IN ('pending', 'approved', 'executing') "
        "THEN 0 ELSE 1 END, COALESCE(executed_at, decided_at, created_at) DESC, id"
    )
    receipt_rows = conn.execute(receipt_query).fetchall()
    for key, action_id, status, created_at, valid_until in receipt_rows:
        conn.execute(
            "INSERT OR IGNORE INTO action_idempotency_receipts "
            "(dedupe_key, action_id, status, created_at, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, action_id, status, created_at, valid_until),
        )

    terminal_expiry = (
        "strftime('%Y-%m-%dT%H:%M:%S', 'now', "
        f"'+{ACTION_RECEIPT_RETENTION_DAYS} days')"
    )
    conn.execute("DROP TRIGGER IF EXISTS pending_actions_receipt_on_insert")
    conn.execute("DROP TRIGGER IF EXISTS pending_actions_receipt_on_status")
    receipt_insert_trigger = (
        "CREATE TRIGGER pending_actions_receipt_on_insert "  # noqa: S608
        "AFTER INSERT ON pending_actions FOR EACH ROW "
        "WHEN NEW.dedupe_key IS NOT NULL "
        "AND NEW.status NOT IN ('rejected', 'expired') BEGIN "
        "INSERT INTO action_idempotency_receipts "
        "(dedupe_key, action_id, status, created_at, valid_until) VALUES ("
        "NEW.dedupe_key, NEW.id, NEW.status, NEW.created_at, "
        "CASE WHEN NEW.status IN ('pending', 'approved', 'executing') THEN NULL "
        f"ELSE {terminal_expiry} END); END"
    )
    conn.execute(receipt_insert_trigger)
    receipt_status_trigger = (
        "CREATE TRIGGER pending_actions_receipt_on_status "  # noqa: S608
        "AFTER UPDATE OF status ON pending_actions FOR EACH ROW BEGIN "
        "DELETE FROM action_idempotency_receipts WHERE action_id=NEW.id "
        "AND NEW.status IN ('rejected', 'expired'); "
        "UPDATE action_idempotency_receipts SET status=NEW.status, valid_until="
        "CASE WHEN NEW.status IN ('pending', 'approved', 'executing') THEN NULL "
        f"ELSE {terminal_expiry} END WHERE action_id=NEW.id; END"
    )
    conn.execute(receipt_status_trigger)


def _migration_action_claim_timestamp(conn: sqlite3.Connection) -> None:
    _migrate_add_column(conn, "pending_actions", "claimed_at TEXT")
    conn.execute(
        "UPDATE pending_actions SET claimed_at=COALESCE(decided_at, created_at) "
        "WHERE status='executing' AND claimed_at IS NULL"
    )


def _migration_garmin_nap_sleep_detail(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garmin_days (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            resting_hr INTEGER,
            sleep_seconds INTEGER,
            sleep_score INTEGER,
            hrv_last_night_avg INTEGER,
            hrv_status TEXT,
            stress_avg INTEGER,
            body_battery_charged INTEGER,
            body_battery_drained INTEGER,
            active_kilocalories INTEGER,
            total_kilocalories INTEGER,
            activities TEXT DEFAULT '[]',
            synced_at TEXT NOT NULL
        )
    """)
    for decl in (
        "nap_seconds INTEGER",
        "nap_start_local TEXT",
        "nap_end_local TEXT",
        "nap_feedback TEXT",
        "deep_sleep_seconds INTEGER",
        "light_sleep_seconds INTEGER",
        "rem_sleep_seconds INTEGER",
        "awake_sleep_seconds INTEGER",
    ):
        _migrate_add_column(conn, "garmin_days", decl)


def _migration_garmin_extended_health(conn: sqlite3.Connection) -> None:
    for decl in (
        "heart_rate_min INTEGER",
        "heart_rate_max INTEGER",
        "heart_rate_samples INTEGER",
        "respiration_lowest REAL",
        "respiration_highest REAL",
        "respiration_awake_avg REAL",
        "respiration_sleep_avg REAL",
        "spo2_avg REAL",
        "spo2_lowest REAL",
        "spo2_sleep_avg REAL",
        "intensity_moderate_minutes INTEGER",
        "intensity_vigorous_minutes INTEGER",
        "intensity_weekly_total INTEGER",
        "intensity_weekly_goal INTEGER",
        "vo2_max REAL",
        "fitness_age REAL",
        "achievable_fitness_age REAL",
        "training_status TEXT",
        "acute_training_load REAL",
        "chronic_training_load REAL",
        "hydration_ml REAL",
        "hydration_goal_ml REAL",
        "weight_kg REAL",
        "bmi REAL",
        "body_fat_pct REAL",
        "body_water_pct REAL",
        "muscle_mass_kg REAL",
        "health_details TEXT DEFAULT '{}'",
    ):
        _migrate_add_column(conn, "garmin_days", decl)


def _migration_legacy_action_repair(conn: sqlite3.Connection) -> None:
    now = utcnow()
    review_until = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', '+7 days')"
    ).fetchone()[0]

    conn.execute("DROP TRIGGER IF EXISTS pending_actions_approval_write_once")

    non_auto_types = (
        "send_draft",
        "edit_vault_file",
        "edit_vault_files",
        "update_calendar_event",
        "create_contact",
        "update_contact",
        "promote_intake",
        "promote_note",
    )

    conn.execute(
        "UPDATE pending_actions SET status='ambiguous', "
        "executed_at=COALESCE(executed_at, ?), "
        "result='legacy approved action had already been attempted; outcome unknown; "
        "reconcile before retry' "
        "WHERE type IN (?,?,?,?,?,?,?,?) AND status='approved' "
        "AND (approval_actor IS NULL OR trim(approval_actor)='') "
        "AND COALESCE(retry_count, 0) > 0",
        (now, *non_auto_types),
    )
    conn.execute(
        "UPDATE pending_actions SET status='pending', approval_channel=NULL, "
        "approval_signal=NULL, approval_actor=NULL, decided_at=NULL, claimed_at=NULL, "
        "executed_at=NULL, result=NULL, retry_count=0, expires_at=? "
        "WHERE type IN (?,?,?,?,?,?,?,?) AND status='approved' "
        "AND (approval_actor IS NULL OR trim(approval_actor)='') "
        "AND COALESCE(retry_count, 0) = 0",
        (review_until, *non_auto_types),
    )

    conn.execute(
        "UPDATE pending_actions SET status='expired', "
        "decided_at=COALESCE(decided_at, ?), "
        "result='mail_draft capability removed; use mail_reply_link' "
        "WHERE type='mail_draft' AND status IN ('pending', 'approved')",
        (now,),
    )
    conn.execute(
        "UPDATE pending_actions SET status='ambiguous', "
        "executed_at=COALESCE(executed_at, ?), "
        "result='mail_draft capability removed while APPEND was executing; "
        "draft may already exist; review Drafts manually' "
        "WHERE type='mail_draft' AND status='executing'",
        (now,),
    )

    _create_action_approval_write_once_trigger(conn)


def _migration_intake_promotion_claim(conn: sqlite3.Connection) -> None:
    _migrate_add_column(conn, "intake", "promotion_claim TEXT")


def _migration_web_search_results(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS web_search_results ("
        "id TEXT PRIMARY KEY, agent TEXT NOT NULL, area TEXT, url TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_results_expires "
        "ON web_search_results(expires_at)"
    )


def _migration_mail_archive_snapshot_repair(conn: sqlite3.Connection) -> None:
    import json

    from src.brain.actions.fingerprints import is_sha256_hex

    def reviewable(raw: str) -> bool:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        string_fields = (
            "provider", "mailbox", "uid", "uidvalidity", "message_id",
            "message_from", "message_subject", "message_header_sha256",
        )
        return (
            isinstance(payload, dict)
            and all(isinstance(payload.get(key), str) for key in string_fields)
            and payload["provider"] in {"icloud", "gmail"}
            and payload["uid"].isdigit()
            and int(payload["uid"]) > 0
            and payload["uid"] == str(int(payload["uid"]))
            and payload["uidvalidity"].isdigit()
            and int(payload["uidvalidity"]) > 0
            and payload["uidvalidity"] == str(int(payload["uidvalidity"]))
            and is_sha256_hex(payload["message_header_sha256"])
            and payload["message_header_sha256"]
            == payload["message_header_sha256"].lower()
        )

    conn.execute("DROP TRIGGER IF EXISTS pending_actions_approval_write_once")
    rows = conn.execute(
        "SELECT id, payload_json, status, COALESCE(retry_count, 0), approval_actor "
        "FROM pending_actions WHERE type='mail_archive' "
        "AND status IN ('pending', 'approved', 'executing')"
    ).fetchall()
    now = utcnow()
    review_until = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', '+7 days')"
    ).fetchone()[0]
    for action_id, raw, status, retries, actor in rows:
        if status == "executing" or (status == "approved" and retries > 0):
            conn.execute(
                "UPDATE pending_actions SET status='ambiguous', executed_at=?, "
                "result='legacy mail archive may already have run; reconcile manually' "
                "WHERE id=?",
                (now, action_id),
            )
        elif not reviewable(raw):
            conn.execute(
                "UPDATE pending_actions SET status='expired', decided_at=?, "
                "result='legacy mail archive lacks a stable message snapshot; re-propose' "
                "WHERE id=?",
                (now, action_id),
            )
        elif status == "approved" and not str(actor or "").strip():
            conn.execute(
                "UPDATE pending_actions SET status='pending', approval_channel=NULL, "
                "approval_signal=NULL, approval_actor=NULL, decided_at=NULL, "
                "claimed_at=NULL, executed_at=NULL, result=NULL, retry_count=0, "
                "expires_at=? WHERE id=?",
                (review_until, action_id),
            )
    _create_action_approval_write_once_trigger(conn)


def _migration_mission_control(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS mc_items ("
        "kind TEXT NOT NULL, id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "state TEXT NOT NULL, state_at TEXT NOT NULL, answer TEXT, result_text TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT, "
        "PRIMARY KEY (kind, id))",
        "CREATE TABLE IF NOT EXISTS mc_action_notes ("
        "action_id TEXT PRIMARY KEY, note_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS mc_holds ("
        "action_id TEXT PRIMARY KEY, response_id TEXT NOT NULL, payload_hash TEXT NOT NULL, "
        "run_after TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS mc_responses ("
        "response_id TEXT PRIMARY KEY, kind TEXT NOT NULL, item_id TEXT NOT NULL, "
        "verb TEXT NOT NULL, outcome TEXT NOT NULL, handled_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS mc_mirror ("
        "path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, written_at TEXT NOT NULL)",
    ):
        conn.execute(statement)


def _migration_mission_control_opened(conn: sqlite3.Connection) -> None:
    _migrate_add_column(conn, "mc_items", "opened_at TEXT")


def _migration_alfred_stage_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vault_edits ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT NOT NULL, "
        "run_label TEXT NOT NULL, tool TEXT NOT NULL, path TEXT NOT NULL, "
        "sha_before TEXT, sha_after TEXT)"
    )
    _migrate_add_column(conn, "findings", "from_outside INTEGER NOT NULL DEFAULT 0")


def _migration_alfred_stage_2_3(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media ("
        "id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent TEXT, run_label TEXT, area TEXT, "
        "place TEXT, kind TEXT, path TEXT, bytes INTEGER NOT NULL, sha256 TEXT, "
        "sent_ts TEXT, sent_channel TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS follow_ups ("
        "id TEXT PRIMARY KEY, source TEXT, ref TEXT, asked_by TEXT, place TEXT, summary TEXT, "
        "status TEXT NOT NULL, first_seen TEXT NOT NULL, last_raised TEXT, research TEXT, "
        "dedupe_key TEXT NOT NULL UNIQUE)"
    )


def _migration_night_shift(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(follow_ups)")}
    for name, kind in (
        ("outcome", "TEXT"),
        ("action_ids", "TEXT"),
        ("dismiss_count", "INTEGER NOT NULL DEFAULT 0"),
        ("night_of", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE follow_ups ADD COLUMN {name} {kind}")  # noqa: S608
    conn.execute(
        "CREATE TABLE IF NOT EXISTS digest_messages ("
        "channel TEXT NOT NULL, ts TEXT NOT NULL, night_of TEXT NOT NULL, "
        "item_ids TEXT NOT NULL, action_ids TEXT NOT NULL, created TEXT NOT NULL, "
        "PRIMARY KEY (channel, ts))"
    )


def _migration_morning_cards(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS morning_cards ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, place TEXT NOT NULL, channel TEXT NOT NULL, "
        "summary_ts TEXT NOT NULL, detail_ts TEXT NOT NULL, area TEXT, follow_up_id TEXT, "
        "card TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL)"
    )

def _migration_smart_reminders(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(follow_ups)")}
    for name in ("remind_at", "reminded_at", "thread_channel", "thread_ts", "reminder_context"):
        if name not in columns:
            conn.execute(f"ALTER TABLE follow_ups ADD COLUMN {name} TEXT")  # noqa: S608
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_remind_at ON follow_ups(remind_at) "
        "WHERE remind_at IS NOT NULL AND reminded_at IS NULL"
    )


def _migration_reminder_requester(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(follow_ups)")}
    if "reminder_requested_by" not in columns:
        conn.execute("ALTER TABLE follow_ups ADD COLUMN reminder_requested_by TEXT")


MIGRATIONS: tuple[tuple[int, str, MigrationFn], ...] = (
    (1, "notifications-routing", _migration_notifications_routing),
    (2, "action-retries", _migration_action_retries),
    (3, "agent-run-telemetry", _migration_agent_run_telemetry),
    (4, "action-provenance", _migration_action_provenance),
    (5, "action-note-task-links", _migration_action_note_task_links),
    (6, "action-reliability-ledger", _migration_action_reliability_ledger),
    (7, "action-claim-timestamp", _migration_action_claim_timestamp),
    (8, "garmin-nap-sleep-detail", _migration_garmin_nap_sleep_detail),
    (9, "legacy-action-repair", _migration_legacy_action_repair),
    (10, "intake-promotion-claim", _migration_intake_promotion_claim),
    (11, "web-search-results", _migration_web_search_results),
    (12, "mail-archive-snapshot-repair", _migration_mail_archive_snapshot_repair),
    (13, "garmin-extended-health", _migration_garmin_extended_health),
    (14, "mission-control", _migration_mission_control),
    (15, "mission-control-opened", _migration_mission_control_opened),
    (16, "alfred-stage-1", _migration_alfred_stage_1),
    (17, "alfred-stage-2-3", _migration_alfred_stage_2_3),
    (18, "night-shift", _migration_night_shift),
    (19, "morning-cards", _migration_morning_cards),
    (20, "smart-reminders", _migration_smart_reminders),
    (21, "reminder-requester", _migration_reminder_requester),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    name: str,
    migrate: MigrationFn,
) -> None:
    row = conn.execute(
        "SELECT name FROM schema_migrations WHERE version=?", (version,)
    ).fetchone()
    if row is not None:
        if row[0] != name:
            raise RuntimeError(
                f"schema migration {version} was recorded as {row[0]!r}, not {name!r}"
            )
        return

    savepoint = f"schema_migration_{version}"
    conn.execute(f"SAVEPOINT {savepoint}")  # noqa: S608
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, utcnow()),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")  # noqa: S608
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")  # noqa: S608
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")  # noqa: S608
        raise


def _apply_migrations(conn: sqlite3.Connection) -> None:
    future = conn.execute(
        "SELECT MAX(version) FROM schema_migrations WHERE version > ?",
        (LATEST_SCHEMA_VERSION,),
    ).fetchone()[0]
    if future is not None:
        raise RuntimeError(
            f"database schema version {future} is newer than supported "
            f"version {LATEST_SCHEMA_VERSION}; refusing to start"
        )
    for version, name, migrate in MIGRATIONS:
        _apply_migration(conn, version, name, migrate)


def _execute_schema_sql(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement schema script without implicit COMMIT.

    ``Connection.executescript`` commits an existing transaction first, which
    would defeat the ``BEGIN IMMEDIATE`` migration lock. ``complete_statement``
    keeps statement parsing in SQLite while every DDL operation remains inside
    the caller's transaction.
    """
    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete base schema SQL")


def _init_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("schema initialization requires a clean connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_schema_sql(conn, """
        CREATE TABLE IF NOT EXISTS findings (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, category TEXT NOT NULL,
            title TEXT NOT NULL, detail TEXT, action TEXT, source_url TEXT,
            confidence TEXT DEFAULT 'MEDIUM', created_at TEXT NOT NULL,
            expires_at TEXT, acknowledged INTEGER DEFAULT 0,
            acted_on INTEGER DEFAULT 0, content_hash TEXT UNIQUE,
            reasoning TEXT);

        CREATE TABLE IF NOT EXISTS knowledge (
            id TEXT PRIMARY KEY, topic TEXT NOT NULL, summary TEXT NOT NULL,
            detail TEXT, source_agent TEXT, source_url TEXT, tags TEXT,
            relevance_score REAL DEFAULT 0.5, first_discovered TEXT NOT NULL,
            last_updated TEXT NOT NULL, last_relevant TEXT,
            recurrence TEXT, next_relevant_date TEXT,
            related_vault_file TEXT, content_hash TEXT UNIQUE);

        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, started_at TEXT,
            completed_at TEXT, status TEXT, findings_count INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0, error TEXT);

        CREATE TABLE IF NOT EXISTS pending_actions (
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
            claimed_at TEXT,
            executed_at TEXT,
            result TEXT,
            expires_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_pending_actions_type ON pending_actions(type);

        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, title TEXT NOT NULL,
            message TEXT NOT NULL, priority TEXT DEFAULT 'default',
            status TEXT NOT NULL, http_status INTEGER DEFAULT 0,
            created_at TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS task_hashes (
            content_hash TEXT PRIMARY KEY,
            normalized_title TEXT NOT NULL,
            original_title TEXT NOT NULL,
            todoist_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_task_hashes_expires ON task_hashes(expires_at);

        CREATE TABLE IF NOT EXISTS daemon_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS intake (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT UNIQUE,
            kind TEXT NOT NULL,
            author_uid TEXT DEFAULT '',
            author_name TEXT DEFAULT '',
            content TEXT NOT NULL,
            meta_json TEXT DEFAULT '',
            trust TEXT NOT NULL DEFAULT 'untrusted',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT '',
            ingested_at TEXT NOT NULL,
            decided_at TEXT,
            promoted_to TEXT,
            promotion_claim TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_intake_status ON intake(status, ingested_at);
        CREATE TABLE IF NOT EXISTS briefs (
            date TEXT PRIMARY KEY,
            composed_at TEXT NOT NULL,
            sections_json TEXT NOT NULL,
            mirrored_at TEXT,
            run_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            subject TEXT,
            trust TEXT,
            payload_hash TEXT,
            detail TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS garmin_days (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            resting_hr INTEGER,
            sleep_seconds INTEGER,
            sleep_score INTEGER,
            hrv_last_night_avg INTEGER,
            hrv_status TEXT,
            stress_avg INTEGER,
            body_battery_charged INTEGER,
            body_battery_drained INTEGER,
            active_kilocalories INTEGER,
            total_kilocalories INTEGER,
            activities TEXT DEFAULT '[]',
            synced_at TEXT NOT NULL
        );
        """)

        _apply_migrations(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def insert_agent_run(
    conn: sqlite3.Connection,
    *,
    agent: str,
    provider: str,
    model: str,
    duration_ms: int = 0,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    findings_count: int = 0,
    status: str = "success",
    started_at: str | None = None,
    completed_at: str | None = None,
    error: str = "",
) -> None:
    now = started_at or utcnow()
    conn.execute(
        "INSERT INTO agent_runs (id, agent, started_at, completed_at, status, "
        "duration_ms, cost_usd, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, tokens_used, findings_count, "
        "provider, model, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            agent, now, completed_at or now, status,
            duration_ms, cost_usd,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            input_tokens + output_tokens,
            findings_count,
            provider, model, error,
        ),
    )
    conn.commit()

def mark_finding(conn: sqlite3.Connection, finding_id: str, action: str):
    if action == "done":
        conn.execute("UPDATE findings SET acted_on=1, acknowledged=1 WHERE id=?", (finding_id,))
    elif action == "skip":
        conn.execute("UPDATE findings SET acted_on=0, acknowledged=1 WHERE id=?", (finding_id,))
    else:
        conn.execute("UPDATE findings SET acknowledged=1 WHERE id=?", (finding_id,))
    conn.commit()


def record_vault_edit(
    *,
    agent: str,
    run_label: str,
    tool: str,
    path: str,
    sha_before: str | None,
    sha_after: str | None,
) -> None:
    """Append one direct vault edit to the ledger.

    `run_label` names the run (its agent and start time). The hashes are the
    SHA-256 of the file's bytes before and after the call; `sha_before` is None
    for a file the call created.
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO vault_edits (ts, agent, run_label, tool, path, sha_before, sha_after) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utcnow(), agent, run_label, tool, path, sha_before, sha_after),
    )
    conn.commit()


def record_event(*, actor: str, action: str, subject: str = "", detail: str = "") -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO events (ts, actor, action, subject, trust, payload_hash, detail) "
        "VALUES (?, ?, ?, ?, 'system', '', ?)",
        (utcnow(), actor, action, subject, detail),
    )
    conn.commit()


def _rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]


def _utc_moment(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def record_media(
    *,
    media_id: str,
    agent: str,
    run_label: str,
    area: str | None,
    place: str,
    kind: str,
    path: str,
    size: int,
    sha256: str,
) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO media (id, ts, agent, run_label, area, place, kind, path, bytes, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (media_id, utcnow(), agent, run_label, area, place, kind, path, size, sha256),
    )
    conn.commit()


def get_media(media_id: str) -> dict | None:
    rows = _rows_as_dicts(get_db().execute("SELECT * FROM media WHERE id=?", (media_id,)))
    return rows[0] if rows else None


def mark_media_sent(media_id: str, *, channel: str, ts: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE media SET sent_ts=?, sent_channel=? WHERE id=?", (ts, channel, media_id)
    )
    conn.commit()


def expire_media(older_than_days: int, *, now: datetime | None = None) -> list[str]:
    """Set `deleted_at` on media older than the cutoff and return the paths it just marked."""
    moment = _utc_moment(now)
    conn = get_db()
    cursor = conn.execute(
        "UPDATE media SET deleted_at=? WHERE deleted_at IS NULL AND ts<? RETURNING path",
        (_stamp(moment), _stamp(moment - timedelta(days=older_than_days))),
    )
    paths = [row[0] for row in cursor.fetchall()]
    conn.commit()
    return paths


def upsert_follow_up(
    *,
    source: str,
    ref: str,
    asked_by: str | None,
    place: str,
    summary: str,
    dedupe_key: str,
    first_seen: str,
) -> str:
    """Add an open follow-up, or refresh the summary of the one with this `dedupe_key`.

    Returns its id, `F-` and six digits.
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO follow_ups "
        "(id, source, ref, asked_by, place, summary, status, first_seen, dedupe_key) "
        "SELECT 'F-' || printf('%06d', COALESCE(MAX(CAST(SUBSTR(id, 3) AS INTEGER)), 0) + 1), "
        "?, ?, ?, ?, ?, 'open', ?, ? FROM follow_ups WHERE true "
        "ON CONFLICT(dedupe_key) DO UPDATE SET summary=excluded.summary",
        (source, ref, asked_by, place, summary, first_seen, dedupe_key),
    )
    follow_up_id = conn.execute(
        "SELECT id FROM follow_ups WHERE dedupe_key=?", (dedupe_key,)
    ).fetchone()[0]
    conn.commit()
    return follow_up_id


def get_follow_up(follow_up_id: str) -> dict | None:
    rows = _rows_as_dicts(
        get_db().execute("SELECT * FROM follow_ups WHERE id=?", (follow_up_id,))
    )
    return rows[0] if rows else None


def list_follow_ups(*, status: str | None = None, since: str | None = None) -> list[dict]:
    """Follow-ups, oldest first; `since` keeps those raised at or after it."""
    cursor = get_db().execute(
        "SELECT * FROM follow_ups "
        "WHERE (?1 IS NULL OR status=?1) AND (?2 IS NULL OR last_raised>=?2) "
        "ORDER BY first_seen, id",
        (status, since),
    )
    return _rows_as_dicts(cursor)


def update_follow_up(
    follow_up_id: str,
    *,
    status: str,
    research: str | None = None,
    summary: str | None = None,
    last_raised: str | None = None,
    expected_status: str | None = None,
) -> bool:
    """Set the status and any field given; return False when nothing was changed."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET status=?, research=COALESCE(?, research), "
        "summary=COALESCE(?, summary), last_raised=COALESCE(?, last_raised) "
        "WHERE id=? AND (? IS NULL OR status=?)",
        (status, research, summary, last_raised, follow_up_id, expected_status, expected_status),
    )
    conn.commit()
    return cursor.rowcount > 0


def raise_follow_up(
    follow_up_id: str,
    *,
    raised_at: str,
    research: str | None = None,
    summary: str | None = None,
) -> bool:
    """Raise an open or raised follow-up unless it was dismissed twice."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET status='raised', last_raised=?, "
        "research=COALESCE(?, research), summary=COALESCE(?, summary) "
        "WHERE id=? AND status IN ('open', 'raised') AND dismiss_count<2",
        (raised_at, research, summary, follow_up_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_follow_up_work(
    follow_up_id: str, *, outcome: str | None, action_ids: list[str] | None, night_of: str,
) -> bool:
    """Record what a night shift did with an item; False when the id is unknown."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET outcome=COALESCE(?, outcome), "
        "action_ids=COALESCE(?, action_ids), night_of=? WHERE id=?",
        (outcome, json.dumps(action_ids) if action_ids is not None else None, night_of, follow_up_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_follow_up_reminder(
    follow_up_id: str, *, remind_at: str, channel: str, thread_ts: str, context: str,
    requested_by: str,
) -> bool:
    """Ask for a reminder in a Slack thread at `remind_at` (UTC); clears an earlier one.

    `requested_by` is the Slack user who asked; the reminder comes back as their message.
    """
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET remind_at=?, reminded_at=NULL, thread_channel=?, thread_ts=?, "
        "reminder_context=?, reminder_requested_by=? WHERE id=? AND status IN ('open', 'raised')",
        (remind_at, channel, thread_ts, context, requested_by, follow_up_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def due_reminders(now: str) -> list[dict]:
    """Open or raised items whose reminder is due and not yet given, oldest first."""
    cursor = get_db().execute(
        "SELECT * FROM follow_ups WHERE remind_at IS NOT NULL AND remind_at<=? "
        "AND reminded_at IS NULL AND status IN ('open', 'raised') ORDER BY remind_at, id",
        (now,),
    )
    return _rows_as_dicts(cursor)


def claim_reminder(follow_up_id: str, now: str) -> bool:
    """Mark a due reminder as given; False when another call already did."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET reminded_at=? WHERE id=? AND reminded_at IS NULL", (now, follow_up_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def add_follow_up_action(follow_up_id: str, action_id: str) -> None:
    """Add one proposal id to an item's `action_ids`, keeping the ones already there."""
    conn = get_db()
    row = conn.execute("SELECT action_ids FROM follow_ups WHERE id=?", (follow_up_id,)).fetchone()
    if row is None:
        return
    ids = json.loads(row[0]) if row[0] else []
    if action_id not in ids:
        ids.append(action_id)
    conn.execute("UPDATE follow_ups SET action_ids=? WHERE id=?", (json.dumps(ids), follow_up_id))
    conn.commit()


def dismiss_follow_up(follow_up_id: str) -> bool:
    """Dismiss an open or raised item and count the dismissal."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET status='dismissed', dismiss_count=dismiss_count+1 "
        "WHERE id=? AND status IN ('open', 'raised')",
        (follow_up_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def snooze_follow_up(follow_up_id: str, *, until: str) -> bool:
    """Keep an open or raised item raised, off the Home tab until the day `until`."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET status='raised', last_raised=? "
        "WHERE id=? AND status IN ('open', 'raised')",
        (until, follow_up_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def _decoded_follow_up(row: dict) -> dict:
    return {**row, "action_ids": json.loads(row["action_ids"]) if row.get("action_ids") else []}


def night_items(night_of: str) -> list[dict]:
    """The items a night shift worked, oldest first, with `action_ids` as a list."""
    cursor = get_db().execute(
        "SELECT * FROM follow_ups WHERE night_of=? ORDER BY first_seen, id", (night_of,)
    )
    return [_decoded_follow_up(row) for row in _rows_as_dicts(cursor)]


def raised_follow_ups() -> list[dict]:
    """Every raised item, oldest first, with `action_ids` as a list."""
    cursor = get_db().execute(
        "SELECT * FROM follow_ups WHERE status='raised' ORDER BY first_seen, id"
    )
    return [_decoded_follow_up(row) for row in _rows_as_dicts(cursor)]


def follow_ups_by_ids(ids: list[str]) -> list[dict]:
    """The items with these ids, in the order given, with `action_ids` as a list."""
    found = {}
    for follow_up_id in ids:
        rows = _rows_as_dicts(get_db().execute("SELECT * FROM follow_ups WHERE id=?", (follow_up_id,)))
        if rows:
            found[follow_up_id] = _decoded_follow_up(rows[0])
    return [found[follow_up_id] for follow_up_id in ids if follow_up_id in found]


def record_card(
    *, place: str, channel: str, summary_ts: str, detail_ts: str, area: str | None,
    follow_up_id: str | None, card: dict,
) -> int:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO morning_cards (place, channel, summary_ts, detail_ts, area, follow_up_id, card, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (place, channel, summary_ts, detail_ts, area, follow_up_id, json.dumps(card), utcnow()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def set_card_detail_ts(card_id: int, detail_ts: str) -> None:
    conn = get_db()
    conn.execute("UPDATE morning_cards SET detail_ts=? WHERE id=?", (detail_ts, card_id))
    conn.commit()


def get_card(card_id: int) -> dict | None:
    rows = _rows_as_dicts(get_db().execute("SELECT * FROM morning_cards WHERE id=?", (card_id,)))
    if not rows:
        return None
    return {**rows[0], "card": json.loads(rows[0]["card"])}


def set_card_payload(card_id: int, card: dict) -> None:
    conn = get_db()
    conn.execute("UPDATE morning_cards SET card=? WHERE id=?", (json.dumps(card), card_id))
    conn.commit()


def handled_cards(since: str) -> list[dict]:
    """Cards created at or after `since` that the owner pressed Done, Tomorrow or Drop on, newest first."""
    rows = _rows_as_dicts(get_db().execute(
        "SELECT place, status, created, card FROM morning_cards "
        "WHERE status IN ('done', 'later', 'dropped') AND created >= ? "
        "AND json_extract(card, '$.area') != '' ORDER BY created DESC, id DESC",
        (since,),
    ))
    return [{**row, "card": json.loads(row["card"])} for row in rows]


def set_card_status(card_id: int, status: str) -> bool:
    conn = get_db()
    cursor = conn.execute("UPDATE morning_cards SET status=? WHERE id=?", (status, card_id))
    conn.commit()
    return cursor.rowcount > 0


def expire_follow_ups(older_than_days: int, *, now: datetime | None = None) -> int:
    """Expire open and raised follow-ups first seen before the cutoff; return how many."""
    cutoff = _stamp(_utc_moment(now) - timedelta(days=older_than_days))
    conn = get_db()
    cursor = conn.execute(
        "UPDATE follow_ups SET status='expired' "
        "WHERE status IN ('open', 'raised') AND first_seen<?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount
