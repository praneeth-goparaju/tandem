from __future__ import annotations

import logging
from typing import Any, TypedDict

log = logging.getLogger("brain.runners.base")

def resolve_agent_name(context: dict, agent_cfg: dict) -> str:
    return context.get("agent_name") or agent_cfg.get("name") or "unknown"


def telemetry_agent_name(agent_name: str, is_reply: bool) -> str:
    """The `agent_runs.agent` value for a row: `<name>` or `<name>_reply`.
    A falsy `agent_name` becomes "unknown" so the row never has a bare
    "_reply" suffix or an empty agent value."""
    if not agent_name:
        agent_name = "unknown"
    return f"{agent_name}_reply" if is_reply else agent_name


def safe_insert_agent_run(**kwargs: Any) -> None:
    """Wrap `src.db.insert_agent_run` so a DB failure logs and returns —
    runners must not crash a successful agent run on telemetry write."""
    try:
        from src.db import get_db, insert_agent_run
        insert_agent_run(get_db(), **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("agent_runs insert failed for %s: %s",
                    kwargs.get("agent", "?"), e)


class AgentRunResult(TypedDict, total=False):
    agent: str
    response: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    provider: str
    model: str
    elapsed_s: float
    duration_ms: int
    status: str
    error: str | None
    tools: int
    turns: int
    stop_reason: str
    turn_limit_reached: bool
    reply_delivered: bool
class ClaudeRuntimeError(Exception):
    ...
