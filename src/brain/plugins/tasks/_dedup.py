from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

_STOP = {"the", "a", "an", "to", "for", "of", "and", "or", "in", "on", "at", "with"}

_HASH_TTL = timedelta(days=30)
_SEQ_THRESHOLD = 0.85
_OVERLAP_THRESHOLD = 0.65


def _tokens(title: str) -> list[str]:
    s = re.sub(r"[^\w\s]", " ", title.lower())
    return [t for t in s.split() if t and t not in _STOP]


def normalize_title(title: str) -> str:
    return " ".join(sorted(_tokens(title)))


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode()).hexdigest()


def fuzzy_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if SequenceMatcher(None, na, nb).ratio() >= _SEQ_THRESHOLD:
        return True
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if len(ta) < 3 or len(tb) < 3:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return overlap >= _OVERLAP_THRESHOLD


def check_local_hash(conn: sqlite3.Connection, h: str) -> dict | None:
    row = conn.execute(
        "SELECT original_title, todoist_id, created_at FROM task_hashes "
        "WHERE content_hash = ? AND expires_at >= ?",
        (h, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")),
    ).fetchone()
    if not row:
        return None
    return {"original_title": row[0], "todoist_id": row[1], "created_at": row[2]}


def record_hash(conn: sqlite3.Connection, title: str, todoist_id: str | None) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "INSERT OR REPLACE INTO task_hashes "
        "(content_hash, normalized_title, original_title, todoist_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            title_hash(title),
            normalize_title(title),
            title,
            todoist_id,
            now.isoformat(),
            (now + _HASH_TTL).isoformat(),
        ),
    )
    conn.commit()


async def dedup_and_add(
    manager,
    title: str,
    *,
    description: str = "",
    project: str | None = None,
    priority: int | None = None,
    due: str | None = None,
    labels: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Three-layer dedup, then create via the given TaskManager.

    Returns {'ok': True, 'deduped': bool, 'result': str[, 'id': str]}.
    Backend errors propagate — callers own the error envelope.
    """
    from src.db import get_db

    conn = get_db()

    prior = check_local_hash(conn, title_hash(title))
    if prior:
        return {
            "ok": True,
            "deduped": True,
            "result": f"DEDUP (local hash): matched prior task '{prior['original_title'][:60]}'",
        }

    for t in await manager.list_pending():
        other = t.get("content") or t.get("title") or ""
        if fuzzy_match(title, other):
            record_hash(conn, title, str(t.get("id", "")) or None)
            return {
                "ok": True,
                "deduped": True,
                "result": f"DEDUP (fuzzy pending): matched '{other[:60]}'",
            }

    for t in await manager.list_completed(limit=100):
        other = t.get("content") or t.get("title") or ""
        if fuzzy_match(title, other):
            record_hash(conn, title, str(t.get("id", "")) or None)
            return {
                "ok": True,
                "deduped": True,
                "result": f"DEDUP (fuzzy completed): matched '{other[:60]}'",
            }

    result = await manager.add(
        title,
        description=description,
        project=project,
        priority=priority,
        due=due,
        labels=labels,
        idempotency_key=idempotency_key,
    )
    record_hash(conn, title, str(result.get("id", "")) or None)
    return {
        "ok": True,
        "deduped": False,
        "id": result.get("id"),
        "result": f"created task id={result.get('id', '?')}",
    }
