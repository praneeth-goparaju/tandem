from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.db import content_hash, get_db, short_id, utcnow

INTAKE_STATUSES = ("pending", "promoted", "dismissed")


def _rows_as_dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def log_event(
    actor: str,
    action: str,
    subject: str | None = None,
    trust: str | None = None,
    payload: str | None = None,
    detail: str = "",
) -> int:
    """Append one row to the events journal; returns the event's integer id.

    The payload itself is not stored — only content_hash(payload), enough to
    correlate journal entries with the data that triggered them.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO events (ts, actor, action, subject, trust, payload_hash, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            actor,
            action,
            subject,
            trust,
            content_hash(payload) if payload is not None else None,
            detail,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def intake_add(
    source: str,
    source_id: str,
    kind: str,
    content: str,
    author_uid: str = "",
    author_name: str = "",
    created_at: str = "",
    meta: dict | None = None,
) -> str | None:
    """Insert one untrusted intake row; returns its id, or None on duplicate source_id."""
    conn = get_db()
    intake_id = short_id()
    cur = conn.execute(
        "INSERT OR IGNORE INTO intake "
        "(id, source, source_id, kind, author_uid, author_name, content, meta_json, "
        "created_at, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intake_id,
            source,
            source_id,
            kind,
            author_uid,
            author_name,
            content,
            json.dumps(meta) if meta else "",
            created_at,
            utcnow(),
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return intake_id


def intake_pending(limit: int = 50, *, exclude_source: str | None = None) -> list[dict]:
    cur = get_db().execute(
        "SELECT * FROM intake WHERE status='pending' AND promotion_claim IS NULL "
        "AND source IS NOT ? ORDER BY ingested_at, rowid LIMIT ?",
        (exclude_source, limit),
    )
    return _rows_as_dicts(cur)


def intake_mark(
    intake_id: str,
    status: str,
    promoted_to: str | None = None,
) -> bool:
    """Decide one unclaimed pending row; return whether this caller won.

    This compare-and-set prevents a dismissal from overwriting an in-flight
    promotion and prevents either decision from rewriting a terminal row.
    """
    if status not in INTAKE_STATUSES:
        raise ValueError(f"invalid intake status {status!r}; expected one of {INTAKE_STATUSES}")
    conn = get_db()
    cur = conn.execute(
        "UPDATE intake SET status=?, decided_at=?, promoted_to=COALESCE(?, promoted_to) "
        "WHERE id=? AND status='pending' AND promotion_claim IS NULL",
        (status, utcnow(), promoted_to, intake_id),
    )
    conn.commit()
    return cur.rowcount == 1


def intake_claim_promotion(intake_id: str, claim_id: str) -> bool:
    if not claim_id:
        raise ValueError("promotion claim id is required")
    conn = get_db()
    cur = conn.execute(
        "UPDATE intake SET promotion_claim=? WHERE id=? AND status='pending' "
        "AND (promotion_claim IS NULL OR promotion_claim=?)",
        (claim_id, intake_id, claim_id),
    )
    conn.commit()
    return cur.rowcount == 1


def intake_release_promotion(intake_id: str, claim_id: str) -> bool:
    """Release a claim only when its action is known to have made no effect."""
    if not claim_id:
        raise ValueError("promotion claim id is required")
    conn = get_db()
    cur = conn.execute(
        "UPDATE intake SET promotion_claim=NULL WHERE id=? AND status='pending' "
        "AND promotion_claim=?",
        (intake_id, claim_id),
    )
    conn.commit()
    return cur.rowcount == 1


def intake_finalize_promotion(
    intake_id: str,
    claim_id: str,
    promoted_to: str,
) -> bool:
    if not claim_id:
        raise ValueError("promotion claim id is required")
    conn = get_db()
    cur = conn.execute(
        "UPDATE intake SET status='promoted', decided_at=?, promoted_to=?, "
        "promotion_claim=NULL WHERE id=? AND status='pending' AND promotion_claim=?",
        (utcnow(), promoted_to, intake_id, claim_id),
    )
    conn.commit()
    return cur.rowcount == 1
