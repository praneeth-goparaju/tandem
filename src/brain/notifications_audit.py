from __future__ import annotations

import logging
from typing import Any

from src.db import get_db, short_id, utcnow

log = logging.getLogger("brain.notifications.audit")


def record_notification(
    *,
    agent: str,
    area: str | None,
    title: str,
    message: str,
    status: str,
    signal_ts: Any,
    finding_id: str | None,
    group_id: str | None,
) -> None:
    ts = str(signal_ts) if signal_ts not in (None, "") else None
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO notifications "
            "(id, agent, title, message, status, finding_id, signal_timestamp, area, group_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (short_id(), agent, title, message[:2000], status, finding_id, ts, area, group_id, utcnow()),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning(f"notifications audit row failed: {e}")
