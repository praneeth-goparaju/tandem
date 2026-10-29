from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from src.brain.actions.types import ActionType
from src.brain.followups.digest import promote_cursor
from src.brain.runtime.spec import RuntimeContext
from src.orchestrator.registry import AGENT_REGISTRY
from src.orchestrator.runners.base import AgentRunResult
from src.orchestrator.runners.claude_runner import USAGE_LIMIT, ClaudeRunner, feature_on

log = logging.getLogger("brain-runner")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_POSTING_TOOLS = frozenset({
    "mcp__brain-tools__push_notify", "mcp__brain-tools__mail_reply_link",
})
_PUSH_ACTION = ActionType.PUSH_NOTIFICATION.value


def _caps(entry: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    tools = entry.get("tools") or []
    can_edit = "Edit" in tools or "Write" in tools
    can_start_helpers = "Agent" in tools and feature_on("BRAIN_HELPERS", environ)
    return {
        "direct_edits": can_edit and feature_on("BRAIN_DIRECT_EDITS", environ),
        "helpers": int(entry.get("max_helpers") or 0) if can_start_helpers else 0,
        "vault_paths": list(entry.get("vault_paths") or []),
    }


def _get_prompt(
    agent_name: str,
    runtime_ctx: RuntimeContext | None = None,
    caps: dict[str, Any] | None = None,
) -> str:
    from src.brain.runtime.loader import load_all
    from src.brain.runtime.template import build_context, build_env, render_agent

    cfg = load_all(_REPO_ROOT)
    if agent_name not in cfg.agents:
        raise KeyError(f"agent '{agent_name}' not found in config/agents/")
    spec = cfg.agents[agent_name]
    env = build_env(_REPO_ROOT / "config")
    rt = runtime_ctx or {}
    ctx = build_context(
        cfg.profile,
        cfg.sources,
        active_mode=rt.get("active_mode"),
        expected_agents=rt.get("expected_agents"),
        expected_area_owners=rt.get("expected_area_owners"),
        caps=caps,
    )
    rendered = render_agent(env, spec.body, ctx)

    try:
        from src.brain.runtime import vault_instructions
        from src.orchestrator import config as _oc

        section = vault_instructions.render_section(
            agent_name,
            vault_path=_oc.VAULT_PATH,
            user_name=(cfg.profile.get("user") or {}).get("name") or "the user",
            config=cfg.vault_instructions,
        )
    except Exception:  # noqa: BLE001
        section = ""
    if section:
        rendered = rendered.rstrip() + "\n\n" + section
    return rendered


_BEFORE_STEP_ENV = (
    "HOME", "USER_DIR", "BRAIN_VERIFY_RUN", "MEMORY_DB_PATH", "VAULT_DB_PATH", "VAULT_PATH",
    "BRAIN_SLACK_OFF",
)
_STDERR_TAIL = 500
_FEATURE_AGENTS = {"follow_up": "BRAIN_FOLLOWUPS"}
_FOLLOW_UP_COLLECTOR = "src.brain.followups.collect"


def run_before_step(module: str, *, repo_root: Path, timeout: float = 120.0) -> int:
    env = {name: os.environ[name] for name in _BEFORE_STEP_ENV if name in os.environ}
    env["PATH"] = os.environ.get("PATH", os.defpath)
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-m", module],
            cwd=repo_root, env=env, timeout=timeout, capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace")[-_STDERR_TAIL:]
        log.warning(
            "before step %s ran past %.0f seconds; end of its stderr: %s",
            module, timeout, tail or "(empty)",
        )
        return -1
    if completed.returncode != 0:
        tail = (completed.stderr or b"").decode("utf-8", "replace")[-_STDERR_TAIL:]
        log.warning(
            "before step %s exited with %s; end of its stderr: %s",
            module, completed.returncode, tail or "(empty)",
        )
    return completed.returncode


async def run_single_agent(
    agent_name: str, runtime_ctx: RuntimeContext | None = None,
) -> AgentRunResult:
    """Run one configured agent through the Claude runner.

    When a closing run follows in this cycle, this agent's copy of its config
    loses `push_notify`, `mail_reply_link` and the `push_notification` action
    type, so it cannot post by any route: the closing run writes the cycle's
    messages.
    """
    switch = _FEATURE_AGENTS.get(agent_name)
    if switch and not feature_on(switch, os.environ):
        log.info("agent %s skipped: %s is off", agent_name, switch)
        return {"agent": agent_name, "status": "skipped"}
    config = AGENT_REGISTRY[agent_name]
    closing_agent = (runtime_ctx or {}).get("closing_agent")
    if closing_agent and closing_agent != agent_name:
        config = {
            **config,
            "tools": [t for t in config["tools"] if t not in _POSTING_TOOLS],
            "allowed_action_types": [
                t for t in config.get("allowed_action_types") or [] if t != _PUSH_ACTION
            ],
        }
    if config.get("before"):
        from src.orchestrator.runners.claude_runner import check_subscription_auth, check_verify_run

        check_subscription_auth(os.environ)
        check_verify_run(os.environ)
    before = config.get("before")
    code = None
    if before and not USAGE_LIMIT.active(time.time()):
        code = await asyncio.to_thread(run_before_step, before, repo_root=_REPO_ROOT)
        if code != 0:
            log.warning("before step %s for %s exited with %s", before, agent_name, code)
    trigger = f"Run your checks. Today is {datetime.now().strftime('%Y-%m-%d %H:%M')}."
    ctx: dict = {
        "agent_name": agent_name,
        "system_prompt": _get_prompt(
            agent_name, runtime_ctx=runtime_ctx, caps=_caps(config, os.environ),
        ),
    }
    if config.get("timeout_seconds"):
        ctx["timeout"] = config["timeout_seconds"]
    result = await ClaudeRunner().run(config, trigger, ctx)
    if before == _FOLLOW_UP_COLLECTOR and code == 0 and result.get("status") == "success":
        await asyncio.to_thread(_commit_follow_up_cursor)
    return result


def _commit_follow_up_cursor() -> None:
    from src.brain.runtime.loader import resolve_user_dir

    promote_cursor(resolve_user_dir(_REPO_ROOT))
