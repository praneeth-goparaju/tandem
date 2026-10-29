from __future__ import annotations

from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _slack_channel_overrides(integrations: dict[str, Any]) -> dict[str, str]:
    notifier = integrations.get("notifier") or {}
    if not isinstance(notifier, dict):
        return {}
    options = notifier.get("options") or {}
    if not isinstance(options, dict):
        return {}
    channels = options.get("channels") or {}
    if not isinstance(channels, dict):
        return {}

    overrides: dict[str, str] = {}
    for name, entry in channels.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        channel_id = entry.get("channel_id")
        if isinstance(channel_id, str) and channel_id.strip():
            overrides[name] = channel_id.strip()
    return overrides


def _build_config(cfg: Any | None = None) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    if cfg is None:
        from src.brain.runtime.loader import load_all

        cfg = load_all(_REPO_ROOT)

    agents: dict[str, dict[str, Any]] = {}
    channel_overrides = _slack_channel_overrides(cfg.integrations)
    push_tools = {
        "mcp__brain-tools__push_notify",
        "mcp__brain-tools__reply_to_message",
    }
    for name, spec in cfg.agents.items():
        effective = list(dict.fromkeys([*spec.builtins, *spec.mcp_tools]))
        reply_tools = list(dict.fromkeys(spec.reply_mcp_tools))

        has_slack_delivery = bool(spec.delivery and spec.delivery.slack)
        if not has_slack_delivery:
            effective = [tool for tool in effective if tool not in push_tools]
            reply_tools = [tool for tool in reply_tools if tool not in push_tools]

        identity = spec.identity.model_dump() if spec.identity else None
        delivery = spec.delivery.model_dump(exclude_none=True) if spec.delivery else None
        override_id = channel_overrides.get(name)
        if has_slack_delivery and override_id and delivery is not None:
            delivery["slack"]["channel_id"] = override_id
        agents[name] = {
            "model": spec.model,
            "tools": effective,
            "reply_tools": reply_tools,
            "allowed_action_types": list(spec.allowed_action_types),
            "max_turns": spec.max_turns,
            "effort": spec.effort,
            "kind": spec.kind,
            "identity": identity,
            "delivery": delivery,
            "reply_brief": spec.reply_brief,
            "chat": spec.chat,
            "vault_paths": list(spec.vault_paths),
            "max_helpers": spec.max_helpers,
            "before": spec.before,
            "timeout_seconds": spec.timeout_seconds,
        }

    run_modes: dict[str, dict[str, Any]] = {}
    for name, mode in cfg.modes.items():
        entry: dict[str, Any] = {
            "parallel": list(mode.parallel),
            "sequential": list(mode.sequential),
        }
        if mode.pre:
            entry["pre"] = list(mode.pre)
        if mode.chain:
            entry["chain"] = mode.chain
        run_modes[name] = entry

    return agents, run_modes, cfg.modes


AGENT_REGISTRY, RUN_MODES, MODE_SPECS = _build_config()
