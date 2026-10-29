"""Close a follow-up item everywhere it shows: its row, and the Apple Reminders it set.

Every path that closes an item (a card button, a "done F-…" reply, a chat or
the owner's own Claude session through `follow_ups_update`) calls
`close_follow_up`, so Done in one place is Done in all of them.
"""

from __future__ import annotations

import asyncio
import logging
import re

from src.brain.actions import queue

log = logging.getLogger("brain.followups.close")

CLOSING = ("done", "dismissed")
_REMINDER_ID = re.compile(r"created reminder id=(\S+)")


def linked_reminder_ids(item: dict) -> list[str]:
    """The Apple Reminders ids of the reminders this item's proposals created."""
    raw = item.get("action_ids") or []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    found = []
    for action_id in raw:
        row = queue.get(str(action_id))
        if row is None or row.get("type") != "create_reminder" or row.get("status") not in ("executed", "partial"):
            continue
        match = _REMINDER_ID.search(str(row.get("result") or ""))
        if match:
            found.append(match.group(1).rstrip(",.;"))
    return list(dict.fromkeys(found))


async def _tick(reminder_id: str, follow_up_id: str) -> None:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import propose_action

    caller = ToolCaller(
        agent="follow_ups", area="alfred_private", allowed_action_types=frozenset({"complete_reminder"}),
    )
    with caller_scope(caller):
        result = await propose_action({
            "type": "complete_reminder",
            "payload": {"id": reminder_id},
            "rationale": f"{follow_up_id} was closed",
        })
    if result.get("is_error"):
        log.warning("reminder %s for %s was not ticked: %s", reminder_id, follow_up_id, result)


def _close_row(follow_up_id: str, status: str, actor: str) -> dict | None:
    import src.db as db

    item = db.get_follow_up(follow_up_id)
    if item is None or item["status"] not in ("open", "raised"):
        return None
    if status == "dismissed":
        changed = db.dismiss_follow_up(follow_up_id)
    else:
        changed = db.update_follow_up(follow_up_id, status="done", expected_status=item["status"])
    if not changed:
        return None
    try:
        db.record_event(actor=actor, action="follow_up_closed", subject=follow_up_id, detail=status)
    except Exception as exc:  # noqa: BLE001
        log.warning("follow_up_closed for %s not journaled: %s", follow_up_id, exc)
    return item


async def close_follow_up(follow_up_id: str, status: str, *, actor: str) -> bool:
    """Set an open or raised item done or dismissed and tick its linked reminders.

    Returns False when the item is unknown or already closed; nothing else changes then.
    The database work runs in a worker thread, off the event loop.
    """
    if status not in CLOSING:
        raise ValueError(f"status must be one of {', '.join(CLOSING)}")
    item = await asyncio.to_thread(_close_row, follow_up_id, status, actor)
    if item is None:
        return False
    for reminder_id in await asyncio.to_thread(linked_reminder_ids, item):
        await _tick(reminder_id, follow_up_id)
    return True
