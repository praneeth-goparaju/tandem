from __future__ import annotations

import hashlib
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
from src.db import get_db
from src.memory import store


def intake_snapshot(content: str, source: object, source_id: object) -> dict[str, Any]:
    encoded = content.encode()
    return {
        "intake_sha256": hashlib.sha256(encoded).hexdigest(),
        "intake_size": len(encoded),
        "intake_source": str(source),
        "intake_source_id": str(source_id),
        "intake_preview": " ".join(content.split())[:180],
    }


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    intake_id = (payload.get("intake_id") or "").strip()
    if not intake_id:
        return {"ok": False, "result": "missing intake_id"}

    action_id = str(payload.get("_action_id") or "").strip()
    claim_id = action_id or f"direct:{intake_id}"

    if not store.intake_claim_promotion(intake_id, claim_id):
        current = get_db().execute(
            "SELECT status, promoted_to, promotion_claim FROM intake WHERE id=?",
            (intake_id,),
        ).fetchone()
        if current is None:
            return permanent_failure(f"no intake row with id '{intake_id}'")
        status, promoted_to, existing_claim = current
        if status == "promoted":
            return {
                "ok": True,
                "result": f"intake {intake_id} already promoted to {promoted_to}",
            }
        if status == "dismissed":
            return permanent_failure(f"intake {intake_id} was dismissed and cannot be promoted")
        return permanent_failure(
            f"intake {intake_id} is already reserved for promotion by "
            f"{existing_claim or 'another action'}"
        )

    conn = get_db()
    row = conn.execute(
        "SELECT content, source, source_id, ingested_at FROM intake "
        "WHERE id=? AND status='pending' AND promotion_claim=?",
        (intake_id, claim_id),
    ).fetchone()
    if row is None:
        return permanent_failure(
            f"intake {intake_id} promotion claim was lost before publication"
        )
    content, source, source_id, ingested_at = row
    snapshot = intake_snapshot(content, source, source_id)
    if any(payload.get(key) != value for key, value in snapshot.items()):
        store.intake_release_promotion(intake_id, claim_id)
        return permanent_failure(
            f"intake {intake_id} changed after approval; review it again"
        )

    first_line = content.strip().splitlines()[0].strip() if content.strip() else ""
    stable_title = first_line[:80] or f"Intake {intake_id}"
    topic = (payload.get("topic") or "").strip() or stable_title

    from src.orchestrator import config

    date_str = str(ingested_at)[:10]
    try:
        vault_root, path = promotion_path(
            vault_root=Path(config.VAULT_PATH),
            configured_notes=Path(config.AGENT_NOTES_DIR),
            date_str=date_str,
            title=stable_title,
            identity=intake_id,
            body=content,
        )
    except (OSError, ValueError):
        store.intake_release_promotion(intake_id, claim_id)
        return permanent_failure(
            "invalid promotion date/destination; agent notes must stay inside the configured vault"
        )

    footer = f"Source: intake {intake_id} ({source}/{source_id})"
    body = content.rstrip("\n") + "\n\n---\n" + footer + "\n"
    try:
        created = publish_once(path, body)
    except PromotionConflict as e:
        store.intake_release_promotion(intake_id, claim_id)
        return permanent_failure(str(e))
    except OSError as e:
        return retryable_failure(f"vault note write failed: {e}")

    ref = f"vault:{path.relative_to(vault_root)}"

    try:
        log_event_once(
            actor="intake_triage",
            action="intake_promoted",
            subject=intake_id,
            trust="untrusted",
            payload=content,
            detail=ref,
        )
        finalized = store.intake_finalize_promotion(intake_id, claim_id, ref)
    except (OSError, sqlite3.Error) as exc:
        return retryable_failure(f"intake published but reconciliation failed: {exc}")
    if not finalized:
        return partial_success(
            f"intake note published to {ref}, but its quarantine row could not be "
            "finalized; manual reconciliation required"
        )
    verb = "promoted" if created else "already promoted"
    return {"ok": True, "result": f"intake {intake_id} {verb} to {ref} ('{topic}')"}
