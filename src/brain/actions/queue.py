from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from src.brain.actions.approval_review import build_approval_review
from src.brain.actions.types import HUMAN_APPROVAL_CHANNELS, Action
from src.db import get_db, utcnow

log = logging.getLogger(__name__)

_COLS = (
    "id", "type", "payload_json", "payload_hash", "dedupe_key", "version",
    "occurrence_key", "rationale", "source_finding_ids", "risk", "status",
    "approval_channel", "approval_signal", "approval_actor", "origin_agent",
    "origin_area", "approval_audience", "created_at", "decided_at", "claimed_at",
    "executed_at", "result", "expires_at", "retry_count",
)
_COLS_SQL = ", ".join(_COLS)

_ACTIVE_STATUSES = ("pending", "approved", "executing")


class DuplicateActionError(RuntimeError):
    def __init__(self, existing_id: str | None, existing_status: str | None) -> None:
        self.existing_id = existing_id
        self.existing_status = existing_status
        suffix = f" ({existing_id})" if existing_id else ""
        status = f" in status {existing_status!r}" if existing_status else ""
        super().__init__(f"duplicate action{suffix}{status}")


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return dict(zip(_COLS, row, strict=True))


def insert(action: Action) -> None:
    row = action.to_row()
    conn = get_db()
    conn.execute(
        "DELETE FROM action_idempotency_receipts "
        "WHERE valid_until IS NOT NULL AND valid_until <= ?",
        (utcnow(),),
    )
    placeholders = ",".join(["?"] * len(row))
    try:
        conn.execute(
            f"INSERT INTO pending_actions ({','.join(row.keys())}) VALUES ({placeholders})",  # noqa: S608
            tuple(row.values()),
        )
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            "SELECT id, status FROM pending_actions WHERE dedupe_key=? "
            "AND status IN (?,?,?) LIMIT 1",
            (row["dedupe_key"], *_ACTIVE_STATUSES),
        ).fetchone()
        if existing is None:
            existing = conn.execute(
                "SELECT action_id, status FROM action_idempotency_receipts "
                "WHERE dedupe_key=? AND (valid_until IS NULL OR valid_until > ?) "
                "LIMIT 1",
                (row["dedupe_key"], utcnow()),
            ).fetchone()
        conn.rollback()
        if existing:
            raise DuplicateActionError(existing[0], existing[1]) from exc
        raise
    conn.commit()


def get(action_id: str) -> dict[str, Any] | None:
    conn = get_db()
    r = conn.execute(
        f"SELECT {_COLS_SQL} FROM pending_actions WHERE id=?", (action_id,)  # noqa: S608
    ).fetchone()
    return _row_to_dict(r) if r else None


def record_review_delivery(
    action_id: str,
    payload_hash: str,
    area: str,
    channel_id: str,
    message_ts: str,
) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO events (ts, actor, action, subject, trust, payload_hash, detail) "
        "VALUES (?, ?, 'approval_review_delivered', ?, 'trusted', ?, ?)",
        (
            utcnow(),
            f"slack_notifier:{area}",
            action_id,
            payload_hash,
            json.dumps({"channel": channel_id, "message_ts": message_ts}, sort_keys=True),
        ),
    )
    conn.commit()


def review_was_delivered(action_id: str, payload_hash: str, area: str) -> bool:
    return get_db().execute(
        "SELECT 1 FROM events WHERE actor=? "
        "AND action='approval_review_delivered' AND subject=? "
        "AND trust='trusted' AND payload_hash=? LIMIT 1",
        (f"slack_notifier:{area}", action_id, payload_hash),
    ).fetchone() is not None


def list_by_status(
    status: str,
    limit: int = 200,
    *,
    origin_agent: str | None = None,
    origin_area: str | None = None,
    origin_areas: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    conn = get_db()
    where = ["status=?"]
    values: list[Any] = [status]
    if origin_areas is not None:
        where.append(f"origin_area IN ({','.join('?' * len(origin_areas))})")
        values.extend(origin_areas)
    elif origin_area is not None:
        where.append("origin_area=?")
        values.append(origin_area)
    elif origin_agent is not None:
        where.append("origin_agent=?")
        values.append(origin_agent)
    values.append(limit)
    rows = conn.execute(
        f"SELECT {_COLS_SQL} FROM pending_actions WHERE {' AND '.join(where)} "  # noqa: S608
        "ORDER BY created_at ASC LIMIT ?",
        tuple(values),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def transition_status(
    action_id: str,
    expected_from: str,
    new_status: str,
    *,
    channel: str | None = None,
    signal: str | None = None,
    actor: str | None = None,
    result: str | None = None,
    expected_origin_area: str | None = None,
    enforce_origin_area: bool = False,
) -> bool:
    """Atomically move an action from `expected_from` to `new_status`.

    Returns True if the row was updated, False if it was already in a
    different state (race-safe approval).
    """
    conn = get_db()

    if (
        expected_from == "pending"
        and new_status == "approved"
        and channel in HUMAN_APPROVAL_CHANNELS
    ):
        row = get(action_id)
        if row is not None and row.get("status") == expected_from:
            review = build_approval_review(row)
            if not review.approvable:
                log.warning(
                    "human approval denied for unreviewable action %s: %s",
                    action_id,
                    review.reason,
                )
                return False
            origin_area = row.get("origin_area")
            if (
                origin_area
                and channel in {"slack_reply", "slack_button"}
                and not review_was_delivered(
                    action_id, str(row.get("payload_hash") or ""), str(origin_area)
                )
            ):
                log.warning(
                    "human approval denied without an exact delivered review: %s",
                    action_id,
                )
                return False

    now = utcnow()
    sets = ["status=?"]
    vals: list[Any] = [new_status]

    if new_status in {"approved", "rejected", "expired"}:
        sets.append("decided_at=?")
        vals.append(now)
    if new_status == "executing":
        sets.append("claimed_at=?")
        vals.append(now)
    if new_status in {"executed", "partial", "ambiguous", "failed"}:
        sets.append("executed_at=?")
        vals.append(now)
    if channel is not None:
        sets.append("approval_channel=?")
        vals.append(channel)
    if signal is not None:
        sets.append("approval_signal=?")
        vals.append(signal)
    if actor is not None:
        sets.append("approval_actor=?")
        vals.append(actor)
    if result is not None:
        sets.append("result=?")
        vals.append(result)

    vals.extend([action_id, expected_from])
    origin_clause = ""
    if enforce_origin_area:
        if expected_origin_area is None:
            origin_clause = " AND origin_area IS NULL"
        else:
            origin_clause = " AND origin_area=?"
            vals.append(expected_origin_area)
    cur = conn.execute(
        f"UPDATE pending_actions SET {','.join(sets)} "  # noqa: S608
        f"WHERE id=? AND status=?{origin_clause}",
        tuple(vals),
    )
    conn.commit()
    return cur.rowcount == 1
