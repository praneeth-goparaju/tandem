from __future__ import annotations

import re
import uuid
from typing import Any

from src.brain.actions import task_links
from src.brain.actions.outcomes import ambiguous_failure, failure_from_exception
from src.brain.mcp._common import tasks
from src.brain.plugins.base import TaskCreateOutcomeUnknown
from src.brain.plugins.tasks._dedup import dedup_and_add

_ACTION_NOTE_ID_RE = re.compile(r"^A-\d{8}-\d{2}$")
_TASK_CREATE_NAMESPACE = uuid.UUID("2da67900-0b96-5d32-8d2d-b18698380e50")


def _create_key(action_id: str) -> str | None:
    if not action_id:
        return None
    return str(uuid.uuid5(_TASK_CREATE_NAMESPACE, action_id))


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    title = (payload.get("content") or "").strip()
    if not title:
        return {"ok": False, "result": "missing content"}
    project_id = payload.get("project_id")
    if not project_id:
        return {"ok": False, "result": "missing project_id (Inbox forbidden)"}
    action_note_id = str(payload.get("action_note_id") or "").strip().upper()
    if action_note_id and not _ACTION_NOTE_ID_RE.fullmatch(action_note_id):
        return {"ok": False, "result": "invalid action_note_id"}
    action_id = str(payload.get("_action_id") or "").strip()

    due = payload.get("due_date") or payload.get("due_string")
    try:
        out = await dedup_and_add(
            tasks(),
            title,
            description=payload.get("description", ""),
            project=project_id,
            priority=payload.get("priority"),
            due=due,
            labels=payload.get("labels"),
            idempotency_key=_create_key(action_id),
        )
    except TaskCreateOutcomeUnknown as e:
        return ambiguous_failure(f"task create outcome unknown: {e}")
    except Exception as e:  # noqa: BLE001
        return failure_from_exception("task backend error", e)

    task_id = out.get("id")
    if action_note_id and out.get("deduped"):
        return {
            "ok": False,
            "result": (
                "task was deduplicated; no exact backend task association was "
                "created for the action note"
            ),
        }
    if action_note_id and not task_id:
        return {
            "ok": False,
            "result": "task created but backend returned no id; action-note link not recorded",
        }
    if action_note_id:
        try:
            task_links.link(action_note_id, str(task_id))
        except ValueError as exc:
            return {"ok": False, "result": f"task created but link failed: {exc}"}

    return {"ok": bool(out.get("ok", True)), "result": out["result"]}
