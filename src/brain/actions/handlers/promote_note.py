from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.brain.actions.handlers._promotion import (
    PromotionConflict,
    log_event_once,
    promotion_path,
    publish_once,
)
from src.brain.actions.outcomes import (
    partial_success,
    permanent_failure,
    retryable_failure,
)
from src.db import utcnow


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    body_text = payload.get("body") or ""
    if not body_text.strip():
        return {"ok": False, "result": "missing body"}

    first_line = body_text.strip().splitlines()[0].strip() if body_text.strip() else ""
    title = (payload.get("title") or "").strip() or first_line[:80] or "Note"
    area = (payload.get("_origin_area") or payload.get("area") or "").strip()
    agent = (
        payload.get("_origin_agent")
        or payload.get("agent")
        or payload.get("proposing_agent")
        or ""
    ).strip() or "agent"
    action_id = (payload.get("_action_id") or "").strip()

    from src.orchestrator import config

    action_date = str(payload.get("_action_created_at") or utcnow())[:10]
    try:
        vault_root, path = promotion_path(
            vault_root=Path(config.VAULT_PATH),
            configured_notes=Path(config.AGENT_NOTES_DIR),
            date_str=action_date,
            title=title,
            identity=action_id,
            body=body_text,
        )
    except (OSError, ValueError):
        return permanent_failure(
            "invalid promotion date/destination; agent notes must stay inside the configured vault"
        )
    date_str = action_date

    footer = f"Promoted by {agent} on {date_str}"
    if area:
        footer += f", area {area}"
    if action_id:
        footer += f", action {action_id}"
    body = body_text.rstrip("\n") + "\n\n---\n" + footer + "\n"
    try:
        created = publish_once(path, body)
    except PromotionConflict as e:
        return permanent_failure(str(e))
    except OSError as e:
        return retryable_failure(f"vault note write failed: {e}")

    ref = f"vault:{path.relative_to(vault_root)}"

    try:
        log_event_once(
            actor=agent,
            action="note_promoted",
            subject=title,
            trust="trusted",
            payload=body_text,
            detail=ref,
        )
    except (OSError, sqlite3.Error) as exc:
        return partial_success(
            f"note published to {ref}, but audit recording failed: "
            f"{type(exc).__name__}: {exc}; manual audit reconciliation required"
        )
    verb = "promoted note to" if created else "note already promoted to"
    return {"ok": True, "result": f"{verb} {ref} ('{title}')"}
