from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape


def build_env(config_dir: Path) -> Environment:
    agents_dir = config_dir / "agents"
    return Environment(
        loader=FileSystemLoader(str(agents_dir)),
        autoescape=select_autoescape([]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )


def build_context(
    profile: dict[str, Any],
    sources: dict[str, str],
    *,
    at: datetime | None = None,
    active_mode: str | None = None,
    expected_agents: list[str] | None = None,
    expected_area_owners: list[str] | None = None,
    caps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the variable context for a prompt render.

    Args:
        profile: parsed user/profile.yaml (dict with 'user', 'family', etc.)
        sources: parsed user/sources.yaml (flat map of logical name → vault path)
        at: override current time (for tests / snapshots)
        active_mode: the orchestration mode currently firing (e.g. "all_morning",
            "scout"). None when called outside a runner cycle (tests, ad-hoc).
        expected_agents: agents the runner intends to dispatch this cycle, in
            order: pre + parallel + sequential.
        expected_area_owners: subset of expected_agents whose `kind` is
            `area_owner`.
        caps: what this run may do, so its prompt describes only abilities it
            has: `direct_edits` (bool), `helpers` (how many it may start) and
            `vault_paths` (the folders and files it may edit). The executor
            fills it from the agent's grants and the feature switches; None
            means nothing is allowed.
    """
    at = at or datetime.now()
    d = at.date()
    ctx: dict[str, Any] = dict(profile)
    ctx.update(
        {
            "today": d.isoformat(),
            "yesterday": (d - timedelta(days=1)).isoformat(),
            "today_year": d.year,
            "today_month": f"{d.month:02d}",
            "weekday": at.strftime("%A"),
            "now": at.strftime("%H:%M"),
            "source": _source_func(sources),
            "active_mode": active_mode or "",
            "expected_agents": list(expected_agents or []),
            "expected_area_owners": list(expected_area_owners or []),
            "caps": caps or {"direct_edits": False, "helpers": 0, "vault_paths": []},
        }
    )
    return ctx


def _source_func(sources: dict[str, str]):
    def _lookup(key: str) -> str:
        try:
            return sources[key]
        except KeyError as e:
            raise KeyError(f"source file '{key}' not in user/sources.yaml") from e

    return _lookup


def render_agent(env: Environment, body: str, ctx: dict[str, Any]) -> str:
    return env.from_string(body).render(**ctx)
