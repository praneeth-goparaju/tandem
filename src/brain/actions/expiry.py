from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from src.brain.actions import queue
from src.db import get_db, utcnow

log = logging.getLogger(__name__)

STUCK_EXECUTING_CEILING_SECS = 600


def sweep_expired() -> int:
    conn = get_db()
    now = utcnow()
    rows = conn.execute(
        "SELECT id FROM pending_actions WHERE status='pending' "
        "AND expires_at IS NOT NULL "
        "AND (julianday(expires_at) IS NULL OR julianday(expires_at) < julianday(?))",
        (now,),
    ).fetchall()

    n = 0
    for (action_id,) in rows:
        if queue.transition_status(action_id, "pending", "expired"):
            n += 1
    if n:
        log.info("expiry sweeper: %d action(s) expired", n)
    return n


def sweep_stuck_executing(ceiling_seconds: int = STUCK_EXECUTING_CEILING_SECS) -> int:
    conn = get_db()
    cutoff = (datetime.now(UTC) - timedelta(seconds=ceiling_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT id FROM pending_actions WHERE status='executing' "
        "AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    ).fetchall()

    n = 0
    for (action_id,) in rows:
        if queue.transition_status(
            action_id, "executing", "ambiguous",
            result=(f"dead-man: stranded in 'executing' >{ceiling_seconds}s "
                    "(executor likely crashed mid-run) — outcome unknown; reconcile first"),
        ):
            n += 1
    if n:
        log.warning("dead-man sweeper: %d stuck 'executing' action(s) -> ambiguous", n)
    return n
