from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from src.brain.runtime.template import build_context, build_env, render_agent

if TYPE_CHECKING:
    from src.brain.runtime.loader import LoadedConfig

AREA_NOTES_OPEN = "{# area-notes #}"
AREA_NOTES_CLOSE = "{# /area-notes #}"


def area_notes_text(agent_name: str, cfg: LoadedConfig, *, at: datetime | None = None) -> str:
    """The rendered text between the area-notes markers of `agent_name`'s prompt.

    The block is rendered like the rest of the prompt: the same environment, so
    shared fragments can be included, and the same profile and sources. Returns
    an empty string for an unknown agent and when either marker is missing.
    """
    spec = cfg.agents.get(agent_name)
    if spec is None:
        return ""
    start = spec.body.find(AREA_NOTES_OPEN)
    if start < 0:
        return ""
    start += len(AREA_NOTES_OPEN)
    end = spec.body.find(AREA_NOTES_CLOSE, start)
    if end < 0:
        return ""
    env = build_env(cfg.repo_root / "config")
    ctx = build_context(cfg.profile, cfg.sources, at=at)
    return render_agent(env, spec.body[start:end], ctx).strip()
