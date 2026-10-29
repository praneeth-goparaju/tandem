from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.brain.mcp._common import _ok
from src.brain.mcp.tool_registry import brain_tool
from src.db import get_db


@brain_tool(
    name="ops_summary",
    description=(
        "Ops-truth snapshot for the morning brief's opening lines. For the last "
        "`since_hours` (default 24) returns, as JSON in the text block: "
        "agent_runs counts by status plus the failed/timeout runs (agent, "
        "status, detail); the current pending-approvals count; how many notes "
        "were promoted in the window; and events grouped by action. A "
        "failed/timeout run whose agent succeeded again later in the window is "
        "a recovered re-run: it is left out of by_status and failures and "
        "counted under `superseded` instead. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "since_hours": {
                "type": "integer",
                "description": "Look-back window in hours (default: 24).",
            },
        },
    },
)
async def ops_summary(args):
    try:
        since_hours = int(args.get("since_hours") or 24)
    except (TypeError, ValueError):
        since_hours = 24
    if since_hours <= 0:
        since_hours = 24

    conn = get_db()
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    runs = conn.execute(
        "SELECT agent, status, COALESCE(error, ''), COALESCE(started_at, '') "
        "FROM agent_runs WHERE started_at > ? ORDER BY started_at DESC",
        (cutoff,),
    ).fetchall()

    last_success: dict[str, str] = {}
    for agent, status, _error, started_at in runs:
        if status == "success" and agent not in last_success:
            last_success[agent] = started_at

    by_status: dict[str, int] = {}
    failures: list[dict[str, str]] = []
    superseded = 0
    for agent, status, error, started_at in runs:
        broken = status in ("failed", "timeout")
        if broken and last_success.get(agent, "") > started_at:
            superseded += 1
            continue
        key = status or "unknown"
        by_status[key] = by_status.get(key, 0) + 1
        if broken:
            failures.append({"agent": agent, "status": status, "detail": error})

    pending_actions = conn.execute(
        "SELECT COUNT(*) FROM pending_actions WHERE status = 'pending'"
    ).fetchone()[0]

    notes_promoted = conn.execute(
        "SELECT COUNT(*) FROM pending_actions "
        "WHERE type = 'promote_note' AND status = 'executed' AND executed_at > ?",
        (cutoff,),
    ).fetchone()[0]

    events_by_action: dict[str, int] = {}
    for row in conn.execute(
        "SELECT action, COUNT(*) FROM events WHERE ts > ? GROUP BY action",
        (cutoff,),
    ).fetchall():
        events_by_action[row[0]] = row[1]

    summary = {
        "since_hours": since_hours,
        "agent_runs": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "failures": failures,
            "superseded": superseded,
        },
        "pending_actions": pending_actions,
        "notes_promoted": notes_promoted,
        "events": {
            "total": sum(events_by_action.values()),
            "by_action": events_by_action,
        },
    }
    return _ok(json.dumps(summary, indent=2, sort_keys=True))
