from __future__ import annotations

from datetime import date

from src.db import get_db, night_items, raised_follow_ups


def latest_night(today: date) -> date | None:
    row = get_db().execute(
        "SELECT MAX(night_of) FROM follow_ups WHERE night_of<=?", (today.isoformat(),)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def waiting(row: dict, today: date) -> bool:
    return (row.get("last_raised") or "")[:10] > today.isoformat()


def needs_and_handled(night_of: date | None, today: date) -> tuple[list[dict], list[dict]]:
    needs = [row for row in raised_follow_ups() if not waiting(row, today)]
    rows = night_items(night_of.isoformat()) if night_of else []
    handled = [row for row in rows if row["status"] == "done" and row.get("outcome") == "done"]
    return needs, handled
