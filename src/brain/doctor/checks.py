from __future__ import annotations

import functools
import hashlib
import imaplib
import os
import re
import sqlite3
import ssl
import stat
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from src.brain.atomic_io import read_regular_text
from src.brain.doctor import Issue, register

_INCLUDE_RE = re.compile(r'{%\s*include\s+"([^"]+)"\s*%}')


@register(severity="error")
def check_jinja_includes(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    config_agents = (repo_root / "config" / "agents").resolve()
    for name, spec in cfg.agents.items():
        for match in _INCLUDE_RE.finditer(spec.body):
            target_rel = match.group(1)
            target = (config_agents / target_rel).resolve()
            if not target.is_relative_to(config_agents):
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message=f"include target '{target_rel}' escapes config/agents/",
                    )
                )
                continue
            if not target.exists():
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message=f"include target not found: {target_rel}",
                    )
                )
    return issues


@register(severity="error")
def check_modes_resolve(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for mode_name, mode in cfg.modes.items():
        for agent_name in mode.agents():
            if agent_name not in cfg.agents:
                issues.append(
                    Issue(
                        file=f"modes.yaml::{mode_name}",
                        message=(
                            f"references unknown agent '{agent_name}' "
                            f"(not in merged registry)"
                        ),
                    )
                )
    return issues


@register(severity="error")
def check_chain_targets(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for mode_name, mode in cfg.modes.items():
        target = getattr(mode, "chain", None)
        if target and target not in cfg.modes:
            issues.append(
                Issue(
                    file=f"modes.yaml::{mode_name}",
                    message=f"chain target '{target}' not found in modes",
                )
            )
    return issues


@register(severity="error")
def check_scheduler_configuration(cfg, repo_root: Path) -> list[Issue]:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from apscheduler.triggers.cron import CronTrigger

    timezone = os.getenv("TIMEZONE", "UTC")
    issues: list[Issue] = []
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        issues.append(Issue(file=".env", message=f"invalid TIMEZONE {timezone!r}"))
    else:
        for name, mode in cfg.modes.items():
            if not mode.schedule:
                continue
            try:
                CronTrigger.from_crontab(mode.schedule, timezone=timezone)
            except (TypeError, ValueError) as exc:
                issues.append(Issue(
                    file=f"modes.yaml::{name}",
                    message=f"invalid cron schedule {mode.schedule!r}: {exc}",
                ))

    raw_timeout = os.getenv("BRAIN_REPLY_TIMEOUT", "180")
    try:
        timeout = int(raw_timeout)
    except ValueError:
        timeout = 0
    if not 5 <= timeout <= 600:
        issues.append(Issue(
            file=".env",
            message="BRAIN_REPLY_TIMEOUT must be an integer from 5 to 600 seconds",
        ))

    for env_name in (
        "AGENT_NOTES_DIR",
        "BRAIN_BACKUP_STAMP_PATH",
        "BRAIN_LOG_PATH",
        "GARMIN_TOKENSTORE",
        "MEMORY_DB_PATH",
        "OCCASIONS_DIR",
        "USER_DIR",
        "VAULT_DB_PATH",
        "VAULT_PATH",
    ):
        raw = os.getenv(env_name, "").strip()
        if raw and not Path(raw).expanduser().is_absolute():
            issues.append(Issue(
                file=".env",
                message=f"{env_name} must be an absolute path when configured",
            ))
    backup_expected = os.getenv("BRAIN_BACKUP_EXPECTED", "").strip()
    if backup_expected not in {"", "0", "1"}:
        issues.append(Issue(
            file=".env",
            message="BRAIN_BACKUP_EXPECTED must be 0 or 1 when configured",
        ))
    return issues


@register(severity="error")
def check_public_discovery_configuration(cfg, repo_root: Path) -> list[Issue]:
    from apscheduler.triggers.cron import CronTrigger

    from src.orchestrator.public_discovery import load_config, public_config_path

    try:
        config = load_config(repo_root)
    except Exception as exc:  # noqa: BLE001
        return [Issue(
            file=str(public_config_path(repo_root)),
            message=f"invalid public discovery configuration: {type(exc).__name__}",
        )]
    if not config.enabled:
        return []
    issues: list[Issue] = []
    if not config.queries:
        issues.append(Issue(
            file=str(public_config_path(repo_root)),
            message="enabled public discovery requires at least one public query",
        ))
    try:
        CronTrigger.from_crontab(config.schedule, timezone=os.getenv("TIMEZONE", "UTC"))
    except (TypeError, ValueError) as exc:
        issues.append(Issue(
            file=str(public_config_path(repo_root)),
            message=f"invalid public discovery schedule: {exc}",
        ))
    if not os.getenv("TAVILY_API_KEY", "").strip():
        issues.append(Issue(
            file=".env",
            message="TAVILY_API_KEY is required when public discovery is enabled",
        ))
    return issues


@register(severity="warn")
def check_agent_web_configuration(cfg, repo_root: Path) -> list[Issue]:
    del cfg, repo_root
    if (
        os.getenv("BRAIN_DOCTOR_SKIP_RUNTIME") == "1"
        or os.getenv("TAVILY_API_KEY", "").strip()
    ):
        return []
    return [Issue(
        file=".env",
        message="TAVILY_API_KEY is not configured; live web research is unavailable",
    )]


@register(severity="error")
def check_vault_runtime_path(cfg, repo_root: Path) -> list[Issue]:
    if os.getenv("BRAIN_DOCTOR_SKIP_RUNTIME") == "1":
        return []

    from src.orchestrator import config

    vault = Path(config.VAULT_PATH)
    try:
        info = vault.lstat()
    except FileNotFoundError:
        return [Issue(
            file="VAULT_PATH",
            message="configured vault directory does not exist",
        )]
    except OSError as exc:
        return [Issue(
            file="VAULT_PATH",
            message=f"configured vault directory cannot be inspected: {type(exc).__name__}",
        )]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [Issue(
            file="VAULT_PATH",
            message="VAULT_PATH must name a real directory, not a symlink or file",
        )]
    if repo_root.resolve().is_relative_to(vault.resolve()):
        return [Issue(
            file="VAULT_PATH",
            message="VAULT_PATH must not contain the application directory",
        )]
    return []


def _integration_choice(value: object) -> tuple[str, dict] | None:
    if isinstance(value, str):
        return value, {}
    if not isinstance(value, dict):
        return None
    name = value.get("type")
    options = value.get("options", {})
    if not isinstance(name, str) or not isinstance(options, dict):
        return None
    if set(value) - {"type", "options"}:
        return None
    return name, options


@register(severity="error")
def check_integration_configuration(cfg, repo_root: Path) -> list[Issue]:
    allowed = {
        "notifier": {"noop", "slack"},
        "tasks": {"noop", "todoist", "apple_reminders"},
    }
    issues: list[Issue] = []
    choices: dict[str, str] = {}
    choice_options: dict[str, dict] = {}
    for key, names in allowed.items():
        value = cfg.integrations.get(key, "noop")
        parsed = _integration_choice(value)
        if parsed is None:
            issues.append(Issue(
                file="config/integrations.yaml",
                message=f"{key} must be a backend name or {{type, options}} mapping",
            ))
            continue
        name, options = parsed
        choices[key] = name
        choice_options[key] = options
        if name not in names:
            issues.append(Issue(
                file="config/integrations.yaml",
                message=f"unknown {key} backend {name!r}",
            ))
        if key == "tasks" and options:
            issues.append(Issue(
                file="config/integrations.yaml",
                message=f"tasks backend {name!r} does not accept options",
            ))

    if choices.get("notifier") == "slack":
        slack_options = choice_options.get("notifier", {})
        allowed_slack_options = {
            "authorized_senders",
            "bot_token",
            "channels",
            "default_channel_id",
            "personas",
            "vault_name",
        }
        unknown = sorted(set(slack_options) - allowed_slack_options)
        if unknown:
            issues.append(Issue(
                file="config/integrations.yaml",
                message=f"unknown Slack options: {', '.join(unknown)}",
            ))
        for variable in (
            "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OWNER_SLACK_USER_ID",
        ):
            option_value = slack_options.get("bot_token") if variable == "SLACK_BOT_TOKEN" else None
            configured = str(option_value or os.getenv(variable, "")).strip()
            if not configured:
                issues.append(Issue(file=".env", message=f"{variable} is required for Slack"))
        senders = slack_options.get("authorized_senders", {})
        if not isinstance(senders, dict):
            issues.append(Issue(
                file="config/integrations.yaml",
                message="Slack options.authorized_senders must be a mapping",
            ))
        else:
            for sender_id, entry in senders.items():
                prefix = f"Slack authorized sender {sender_id!r}"
                if not isinstance(sender_id, str) or not isinstance(entry, dict):
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=f"{prefix} must use an object with name and areas",
                    ))
                    continue
                unknown_sender_fields = sorted(set(entry) - {"name", "areas"})
                if unknown_sender_fields:
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=f"{prefix} has unknown fields: {', '.join(unknown_sender_fields)}",
                    ))
                if not isinstance(entry.get("name"), str) or not entry["name"].strip():
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=f"{prefix} needs a non-empty name",
                    ))
                areas = entry.get("areas")
                if areas == "*":
                    continue
                if not isinstance(areas, list):
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=f"{prefix} areas must be '*' or a list",
                    ))
                    continue
                invalid_areas = [
                    area for area in areas
                    if not isinstance(area, str) or area not in cfg.agents
                ]
                if invalid_areas:
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=f"{prefix} names unregistered areas: {invalid_areas!r}",
                    ))
        channels = slack_options.get("channels", {})
        if not isinstance(channels, dict):
            issues.append(Issue(
                file="config/integrations.yaml",
                message="Slack options.channels must be a mapping",
            ))
            channels = {}
        for area, entry in channels.items():
            if area != "default" and area not in cfg.agents:
                issues.append(Issue(
                    file="config/integrations.yaml",
                    message=f"Slack channel {area!r} does not name a registered agent",
                ))
            elif area != "default":
                spec = cfg.agents[area]
                if not (spec.delivery and spec.delivery.slack):
                    issues.append(Issue(
                        file="config/integrations.yaml",
                        message=(
                            f"Slack channel {area!r} cannot grant delivery to an "
                            "agent without delivery.slack frontmatter"
                        ),
                    ))
            channel_id = entry.get("channel_id") if isinstance(entry, dict) else None
            if not isinstance(channel_id, str) or not channel_id.strip():
                issues.append(Issue(
                    file="config/integrations.yaml",
                    message=f"Slack channel {area!r} needs a non-empty channel_id",
                ))
            elif "EXAMPLE" in channel_id.upper():
                issues.append(Issue(
                    file="config/integrations.yaml",
                    message=f"replace the example Slack channel_id for {area!r}",
                ))
        for name, spec in cfg.agents.items():
            delivery = spec.delivery.slack if spec.delivery else None
            override = channels.get(name)
            override_id = (
                override.get("channel_id") if isinstance(override, dict) else None
            )
            if (
                delivery
                and "EXAMPLE" in delivery.channel_id.upper()
                and not (isinstance(override_id, str) and override_id.strip())
            ):
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message="replace the example Slack channel_id before enabling Slack",
                ))
    if choices.get("tasks") == "todoist" and not os.getenv(
        "TODOIST_API_TOKEN", ""
    ).strip():
        issues.append(Issue(file=".env", message="TODOIST_API_TOKEN is required for Todoist"))
    return issues


@register(severity="error", connector=True)
def check_private_configuration_permissions(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    env_file = repo_root / ".env"
    try:
        info = env_file.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            issues.append(Issue(file=".env", message=".env must be a real regular file"))
        elif hasattr(os, "getuid") and info.st_uid != os.getuid():
            issues.append(Issue(file=".env", message=".env must be owned by the current user"))
        elif stat.S_IMODE(info.st_mode) != 0o600:
            issues.append(Issue(file=".env", message=".env must have mode 0600"))

    from src.brain.runtime.loader import resolve_user_dir

    user_dir = resolve_user_dir(repo_root)
    fixture_root = Path(os.path.abspath(repo_root / "tests" / "fixtures"))
    user_dir_absolute = Path(os.path.abspath(user_dir))
    if user_dir_absolute.is_relative_to(fixture_root):
        return issues
    try:
        root_info = user_dir.lstat()
    except FileNotFoundError:
        return issues
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        issues.append(Issue(file=str(user_dir), message="USER_DIR must be a real directory"))
        return issues

    counts = {"wrong_owner": 0, "unsafe_mode": 0, "symlink": 0, "other": 0}
    paths = [user_dir]
    for current, dirs, files in os.walk(user_dir, followlinks=False):
        paths.extend(Path(current) / name for name in [*dirs, *files])
    for path in paths:
        try:
            value = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode):
            counts["symlink"] += 1
            continue
        is_dir = stat.S_ISDIR(value.st_mode)
        if not is_dir and not stat.S_ISREG(value.st_mode):
            counts["other"] += 1
            continue
        if hasattr(os, "getuid") and value.st_uid != os.getuid():
            counts["wrong_owner"] += 1
        expected = 0o700 if is_dir else 0o600
        if stat.S_IMODE(value.st_mode) != expected:
            counts["unsafe_mode"] += 1
    labels = {
        "wrong_owner": "entries are not owned by the current user",
        "unsafe_mode": "entries do not use directory 0700/file 0600 modes",
        "symlink": "symlinks are not allowed in the private overlay",
        "other": "non-file/non-directory entries are not allowed in the private overlay",
    }
    for key, message in labels.items():
        if counts[key]:
            issues.append(Issue(
                file=str(user_dir),
                message=f"{counts[key]} {message}",
            ))
    return issues


_RETIRED_SYMBOLS = (
    "write_daily_note",
    "post_action",
    "today_iso",
    "weekday_long",
    "post_morning_checklist",
    "record_digest_message",
    "get_digest_message",
    "approve_all_modal",
    "digest_submit",
    "dm_fallback_agent",
    "send_morning_message",
)


@register(severity="error")
def check_claude_runtime(cfg, repo_root: Path) -> list[Issue]:
    if os.getenv("BRAIN_DOCTOR_SKIP_RUNTIME") == "1":
        return []
    pinned = "0.2.160"
    try:
        import claude_agent_sdk
    except ImportError:
        return [Issue(
            file="pyproject.toml",
            message=f"claude-agent-sdk is not installed; run `make install` (pinned {pinned})",
        )]
    issues: list[Issue] = []
    version = getattr(claude_agent_sdk, "__version__", "")
    if version != pinned:
        issues.append(Issue(
            file="pyproject.toml",
            message=f"claude-agent-sdk {version or 'unknown'} is installed; {pinned} is pinned",
        ))
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not bundled.is_file():
        issues.append(Issue(
            file="pyproject.toml",
            message="claude-agent-sdk has no bundled Claude Code CLI; reinstall it",
        ))
    return issues


@register(severity="error")
def check_claude_auth(cfg, repo_root: Path) -> list[Issue]:
    return [
        Issue(
            file=".env",
            message=f"{name} is set; remove it so agents run on the Claude subscription",
        )
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        if os.getenv(name, "").strip()
    ]


@register(severity="error")
def check_agent_tool_allowlists(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.actions.types import ActionType
    from src.brain.mcp.server import load_brain_tools
    from src.orchestrator.runners.claude_runner import SUPPORTED_BUILTINS

    registered = {tool.name for tool in load_brain_tools()}
    supported_builtins = set(SUPPORTED_BUILTINS)
    valid_action_types = {action.value for action in ActionType}
    action_wrapper_types = {
        "push_notify": "push_notification",
        "tasks_create": "create_todoist_task",
        "create_calendar_event": "create_calendar_event",
        "update_calendar_event": "update_calendar_event",
        "mail_archive": "mail_archive",
        "mail_archive_batch": "mail_archive_batch",
        "send_bot_message": "send_bot_message",
        "create_reminder": "create_reminder",
        "create_contact": "create_contact",
        "update_contact": "update_contact",
    }
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        declared_actions = list(spec.allowed_action_types)
        declared_action_set = set(declared_actions)
        unknown_actions = sorted(declared_action_set - valid_action_types)
        for action_type in unknown_actions:
            issues.append(Issue(
                file=f"agents/{name}.md",
                message=f"allowed_action_types contains unknown type '{action_type}'",
            ))
        if len(declared_actions) != len(declared_action_set):
            issues.append(Issue(
                file=f"agents/{name}.md",
                message="allowed_action_types contains duplicate entries",
            ))

        short_tools: set[str] = set()
        for builtin in spec.builtins:
            if builtin not in supported_builtins:
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=f"builtin '{builtin}' is not allowed by the Claude runtime",
                ))
        for tool in [*spec.mcp_tools, *spec.reply_mcp_tools]:
            prefix = "mcp__brain-tools__"
            if not tool.startswith(prefix):
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=f"external/provider-specific tool '{tool}' is unsupported",
                ))
                continue
            short_name = tool.removeprefix(prefix)
            short_tools.add(short_name)
            if short_name not in registered:
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=f"tool '{short_name}' is not registered",
                ))
        if "propose_action" in short_tools and not declared_action_set:
            issues.append(Issue(
                file=f"agents/{name}.md",
                message=(
                    "propose_action requires an explicit non-empty "
                    "allowed_action_types allowlist"
                ),
            ))
        for tool_name, required_action in action_wrapper_types.items():
            if tool_name in short_tools and required_action not in declared_action_set:
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=(
                        f"tool '{tool_name}' requires allowed action type "
                        f"'{required_action}'"
                    ),
                ))
    return issues


@register(severity="error")
def check_kind_contracts(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        kind = spec.kind
        if kind is None:
            issues.append(
                Issue(
                    file=f"agents/{name}.md",
                    message="missing required field: kind (area_owner | service | system)",
                )
            )
            continue
        if kind == "area_owner":
            if '{% include "_area_owner.md" %}' not in spec.body:
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message='kind=area_owner requires {% include "_area_owner.md" %} in prompt body',
                    )
                )
            if not spec.identity:
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message="kind=area_owner requires identity block (display_name, icon_emoji)",
                    )
                )
            if not (spec.delivery and spec.delivery.slack):
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message="kind=area_owner requires delivery.slack.channel_id",
                    )
                )
        elif kind == "system":
            if spec.delivery:
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message="kind=system must NOT have delivery (system agents don't push)",
                    )
                )
    return issues


_PERSONA_NAMES = {"Alfred"}


@register(severity="warn")
def check_persona_consistency(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    intro_re = re.compile(r"I'm (\w[\w '\.]+?)(?:[\.,—\-]| who | the | from )", re.IGNORECASE)
    for name, spec in cfg.agents.items():
        if not spec.identity:
            continue
        expected = spec.identity.display_name
        for m in intro_re.finditer(spec.body):
            captured = m.group(1).strip()
            if captured not in _PERSONA_NAMES:
                continue
            if captured != expected and not expected.startswith(captured) and captured not in expected:
                issues.append(
                    Issue(
                        file=f"agents/{name}.md",
                        message=(
                            f"prompt opens with 'I'm {captured}' but identity.display_name "
                            f"is '{expected}' — persona drift"
                        ),
                    )
                )
                break
    return issues


@register(severity="warn")
def check_channel_format(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    cid_re = re.compile(r"^[CDG][A-Z0-9]+$")
    for name, spec in cfg.agents.items():
        if not (spec.delivery and spec.delivery.slack):
            continue
        cid = spec.delivery.slack.channel_id
        if not cid_re.match(cid):
            issues.append(
                Issue(
                    file=f"agents/{name}.md",
                    message=f"channel_id '{cid}' doesn't match expected Slack format ^[CDG][A-Z0-9]+$",
                )
            )
    return issues


@register(severity="error")
def check_chat_places(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator.registry import _slack_channel_overrides

    overrides = _slack_channel_overrides(cfg.integrations)
    on_channel: dict[str, list[str]] = {}
    with_chat: dict[str, list[str]] = {}
    for name, spec in cfg.agents.items():
        if spec.chat:
            with_chat.setdefault(spec.chat, []).append(name)
        if spec.delivery and spec.delivery.slack:
            channel = overrides.get(name) or spec.delivery.slack.channel_id
            on_channel.setdefault(channel, []).append(name)

    issues: list[Issue] = []
    for channel, names in on_channel.items():
        if len(names) < 2:
            continue
        chat_agents = [name for name in names if cfg.agents[name].chat]
        if not chat_agents:
            issues.append(Issue(
                file=f"agents/{names[0]}.md",
                message=(
                    f"Slack channel {channel} is shared by {', '.join(names)} "
                    "but none of them sets chat (shared or private)"
                ),
            ))
        elif len(chat_agents) > 1:
            issues.append(Issue(
                file=f"agents/{chat_agents[0]}.md",
                message=(
                    f"Slack channel {channel} has more than one chat agent: "
                    f"{', '.join(chat_agents)}"
                ),
            ))
    for place, names in with_chat.items():
        if len(names) > 1:
            issues.append(Issue(
                file=f"agents/{names[0]}.md",
                message=f"chat: {place} is set on more than one agent: {', '.join(names)}",
            ))

    return issues


def _effective_channel(name: str, spec, overrides: dict[str, str]) -> str | None:
    if not (spec.delivery and spec.delivery.slack):
        return None
    return overrides.get(name) or spec.delivery.slack.channel_id or None


def _scheduled_closing_modes(cfg) -> list[str]:
    return [
        name for name, mode in cfg.modes.items()
        if mode.schedule and mode.sequential
        and getattr(cfg.agents.get(mode.sequential[-1]), "chat", None) == "private"
    ]


def _areas_without_place(cfg) -> list[Issue]:
    from src.orchestrator.registry import _slack_channel_overrides

    overrides = _slack_channel_overrides(cfg.integrations)
    places = {
        _effective_channel(name, spec, overrides)
        for name, spec in cfg.agents.items() if spec.chat
    }
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        channel = _effective_channel(name, spec, overrides)
        if channel and channel not in places:
            issues.append(Issue(
                file=f"agents/{name}.md",
                message=(
                    f"area {name} has no place: replies there are refused and it "
                    "cannot appear in a morning message"
                ),
            ))
    return issues


@register(severity="warn")
def check_area_places(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator.registry import _slack_channel_overrides

    issues = [] if _scheduled_closing_modes(cfg) else _areas_without_place(cfg)
    private = [name for name, spec in cfg.agents.items() if spec.chat == "private"]
    if not private:
        issues.append(Issue(
            file="config/agents",
            message="no agent has chat: private, so the owner's direct messages are no place",
        ))
    overrides = _slack_channel_overrides(cfg.integrations)
    for name in private:
        if not _effective_channel(name, cfg.agents[name], overrides):
            issues.append(Issue(
                file=f"agents/{name}.md",
                message=(
                    f"the private chat agent {name} has no Slack channel id, "
                    "so the owner's direct messages are no place"
                ),
            ))
    return issues


@register(severity="error")
def check_area_places_for_a_scheduled_morning(cfg, repo_root: Path) -> list[Issue]:
    return _areas_without_place(cfg) if _scheduled_closing_modes(cfg) else []


def _vault_path_conflict(entry: str, offlimits: list[str]) -> str | None:
    from src.brain.runtime.vault_paths import names_path

    path = PurePosixPath(entry)
    if not path.parts or path.parts == (path.anchor,):
        return "names the whole vault, which holds paths direct edits never touch"
    for blocked in offlimits:
        if names_path(path.parts, blocked, fold=True):
            return f"is, or is inside, '{blocked}', which direct edits never touch"
    held = [
        blocked for blocked in offlimits
        if names_path(PurePosixPath(blocked).parts, entry, fold=True)
    ]
    if held:
        names = ", ".join(f"'{blocked}'" for blocked in held)
        return f"contains {names}, which direct edits never touch"
    return None


@register(severity="error")
def check_vault_paths_offlimits(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator.runners.claude_runner import DEFAULT_DAILY_NOTES_DIR
    from src.orchestrator.runners.run_guard import vault_offlimits

    daily = (cfg.profile.get("notes") or {}).get("daily_notes_dir") or DEFAULT_DAILY_NOTES_DIR
    offlimits = vault_offlimits(cfg.vault_instructions, daily)
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        for entry in spec.vault_paths:
            conflict = _vault_path_conflict(entry, offlimits)
            if conflict:
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=f"vault_paths entry '{entry}' {conflict}",
                ))
    return issues


@register(severity="warn")
def check_vault_paths_folder_slash(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        for entry in spec.vault_paths:
            path = PurePosixPath(entry)
            if path.parts and not entry.endswith("/") and not path.suffix:
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=(
                        f"vault_paths entry '{entry}' has no trailing / and no file "
                        "extension: end a folder with /, or it names one file"
                    ),
                ))
    return issues


@register(severity="error")
def check_area_notes_markers(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime.area_notes import AREA_NOTES_CLOSE, AREA_NOTES_OPEN

    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        if spec.kind != "area_owner":
            continue
        start = spec.body.find(AREA_NOTES_OPEN)
        if start < 0 or spec.body.find(AREA_NOTES_CLOSE, start + len(AREA_NOTES_OPEN)) < 0:
            issues.append(Issue(
                file=f"agents/{name}.md",
                message=(
                    f"kind=area_owner needs {AREA_NOTES_OPEN} before "
                    f"{AREA_NOTES_CLOSE} in the prompt body"
                ),
            ))
    return issues


_MORNING_TOOLS = ("mcp__brain-tools__send_morning_cards",)


@register(severity="error")
def check_morning_tool_grant(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for name, spec in cfg.agents.items():
        for tool in _MORNING_TOOLS:
            if tool in spec.reply_mcp_tools or (tool in spec.mcp_tools and spec.chat != "private"):
                issues.append(Issue(
                    file=f"agents/{name}.md",
                    message=(
                        f"{tool.rsplit('__', 1)[-1]} may be granted only in mcp_tools "
                        "of the agent with chat: private, the closing run"
                    ),
                ))
    return issues


@register(severity="warn")
def check_dead_code(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    src = repo_root / "src"
    if not src.is_dir():
        return issues
    doctor_dir = src / "brain" / "doctor"
    for py in src.rglob("*.py"):
        if py.is_relative_to(doctor_dir):
            continue
        try:
            text = py.read_text()
        except OSError:
            continue
        for ln, line in enumerate(text.splitlines(), start=1):
            for needle in _RETIRED_SYMBOLS:
                if needle in line:
                    rel = py.relative_to(repo_root)
                    issues.append(
                        Issue(
                            file=str(rel),
                            line=ln,
                            message=f"retired symbol '{needle}' reappeared",
                        )
                    )
                    break
    return issues


@register(severity="warn")
def check_doc_drift(cfg, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    docs = ["ARCHITECTURE.md", "INTEGRATIONS.md"]
    scraper_dir = repo_root / "src" / "brain" / "housing" / "scrapers"
    handler_dir = repo_root / "src" / "brain" / "actions" / "handlers"
    known_scrapers = {p.stem for p in scraper_dir.glob("*.py")} if scraper_dir.exists() else set()
    known_handlers = {p.stem for p in handler_dir.glob("*.py")} if handler_dir.exists() else set()

    scraper_pattern = re.compile(
        r"(?:scrapers?|via Playwright on|httpx[- ]?based)[^\n]*?"
        r"\b(immowelt|immoscout24|kleinanzeigen)\b",
        re.IGNORECASE,
    )

    handler_mention = re.compile(r"\b(create_todoist_task|send_draft|edit_vault_file|push_notification)\b")

    for doc in docs:
        path = repo_root / "docs" / doc
        if not path.exists():
            path = repo_root / doc
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            for m in scraper_pattern.finditer(line):
                name = m.group(1).lower()
                if name not in known_scrapers:
                    issues.append(Issue(
                        file=doc, line=ln,
                        message=f"doc names scraper '{name}' but no src/brain/housing/scrapers/{name}.py",
                    ))
            for m in handler_mention.finditer(line):
                if m.group(1) not in known_handlers:
                    issues.append(Issue(
                        file=doc, line=ln,
                        message=f"doc names handler '{m.group(1)}' but no actions/handlers/{m.group(1)}.py",
                    ))
    return issues


_ICLOUD_NETWORK_TIMEOUT = 5.0


def _icloud_creds() -> tuple[str, str]:
    from src.brain.mcp.tools._icloud import _keychain_password

    apple_id = os.getenv("ICLOUD_APPLE_ID", "")
    app_pw = os.getenv("ICLOUD_APP_PASSWORD", "")
    if apple_id and not app_pw:
        app_pw = _keychain_password(apple_id)
    return apple_id, app_pw


@register(severity="warn")
def check_icloud_creds(cfg, repo_root: Path) -> list[Issue]:
    apple_id, app_pw = _icloud_creds()
    if not apple_id and not app_pw:
        return []
    if apple_id and not app_pw:
        return [Issue(
            file=".env",
            message=(
                "ICLOUD_APPLE_ID is set but there is no app password: set "
                "ICLOUD_APP_PASSWORD in .env, or add the Keychain item "
                "brain-icloud-app-password (see docs/INTEGRATIONS.md)"
            ),
        )]
    if app_pw and not apple_id:
        return [Issue(file=".env", message="ICLOUD_APP_PASSWORD set but ICLOUD_APPLE_ID empty")]
    return []


@register(severity="warn")
def check_icloud_caldav_reachable(cfg, repo_root: Path) -> list[Issue]:
    apple_id, app_pw = _icloud_creds()
    if not (apple_id and app_pw):
        return []

    import base64
    import http.client

    auth = base64.b64encode(f"{apple_id}:{app_pw}".encode()).decode()
    try:
        conn = http.client.HTTPSConnection(
            "caldav.icloud.com", 443, timeout=_ICLOUD_NETWORK_TIMEOUT
        )
        try:
            conn.request(
                "PROPFIND",
                "/",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Depth": "0",
                    "Content-Length": "0",
                },
            )
            resp = conn.getresponse()
            status = resp.status
        finally:
            conn.close()
    except (TimeoutError, OSError, ssl.SSLError) as e:
        return [Issue(file=".env", message=f"caldav.icloud.com unreachable: {e}")]

    if status == 401:
        return [Issue(
            file=".env",
            message="CalDAV auth failed (401) — app-specific password revoked or rotated. Mint a new one at https://account.apple.com",
        )]
    if status not in (207, 301, 302):
        return [Issue(file=".env", message=f"CalDAV PROPFIND returned unexpected status {status}")]
    return []


@register(severity="warn")
def check_icloud_imap_reachable(cfg, repo_root: Path) -> list[Issue]:
    apple_id, app_pw = _icloud_creds()
    if not (apple_id and app_pw):
        return []

    import imaplib

    # IMAP wants the @icloud.com mailbox address, not the Apple ID. If
    # the user set the override, use it; otherwise try the Apple ID.
    imap_user = os.getenv("ICLOUD_IMAP_USER") or apple_id

    try:
        with imaplib.IMAP4_SSL("imap.mail.me.com", 993, timeout=_ICLOUD_NETWORK_TIMEOUT) as m:
            try:
                m.login(imap_user, app_pw)
            except imaplib.IMAP4.error as e:
                return [Issue(
                    file=".env",
                    message=f"IMAP auth failed: {e}. App-specific password may be revoked.",
                )]
            try:
                m.logout()
            except imaplib.IMAP4.error:
                pass
    except (TimeoutError, OSError, ssl.SSLError) as e:
        return [Issue(file=".env", message=f"imap.mail.me.com unreachable: {e}")]
    return []


@register(severity="warn")
def check_gmail_imap_reachable(cfg, repo_root: Path) -> list[Issue]:
    addr = os.getenv("GMAIL_ADDRESS", "")
    pw = os.getenv("GMAIL_APP_PASSWORD", "")
    if not (addr and pw):
        return []

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=_ICLOUD_NETWORK_TIMEOUT) as m:
            try:
                m.login(addr, pw)
            except imaplib.IMAP4.error as e:
                return [Issue(
                    file=".env",
                    message=f"Gmail IMAP auth failed: {e}. App password may be revoked, or IMAP/2FA not enabled.",
                )]
            try:
                m.logout()
            except imaplib.IMAP4.error:
                pass
    except (TimeoutError, OSError, ssl.SSLError) as e:
        return [Issue(file=".env", message=f"imap.gmail.com unreachable: {e}")]
    return []


@register(severity="warn")
def check_icloud_carddav_reachable(cfg, repo_root: Path) -> list[Issue]:
    apple_id, app_pw = _icloud_creds()
    if not (apple_id and app_pw):
        return []

    import base64
    import http.client

    auth = base64.b64encode(f"{apple_id}:{app_pw}".encode()).decode()
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:">'
        '<d:prop><d:current-user-principal/></d:prop>'
        '</d:propfind>'
    )
    try:
        conn = http.client.HTTPSConnection(
            "contacts.icloud.com", 443, timeout=_ICLOUD_NETWORK_TIMEOUT
        )
        try:
            conn.request(
                "PROPFIND",
                "/",
                body=body,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Depth": "0",
                    "Content-Type": "application/xml; charset=utf-8",
                },
            )
            resp = conn.getresponse()
            status = resp.status
        finally:
            conn.close()
    except (TimeoutError, OSError, ssl.SSLError) as e:
        return [Issue(file=".env", message=f"contacts.icloud.com unreachable: {e}")]

    if status == 401:
        return [Issue(
            file=".env",
            message="CardDAV auth failed (401) — app-specific password revoked or rotated.",
        )]
    if status not in (207, 301, 302):
        return [Issue(file=".env", message=f"CardDAV PROPFIND returned unexpected status {status}")]
    return []


@register(severity="warn")
def check_icloud_eventkit_access(cfg, repo_root: Path) -> list[Issue]:
    tasks_cfg = cfg.integrations.get("tasks", "noop")
    tasks_name = (
        tasks_cfg.get("type", "noop") if isinstance(tasks_cfg, dict) else tasks_cfg
    )
    if tasks_name != "apple_reminders":
        return []
    if sys.platform != "darwin":
        return [Issue(
            file="config/integrations.yaml",
            message="tasks backend apple_reminders requires macOS",
        )]
    try:
        from src.brain.mcp.tools._icloud import (
            eventkit_store,
        )
    except ImportError as e:
        return [Issue(file=".env", message=f"EventKit dependencies unavailable: {e}")]

    try:
        eventkit_store()
    except RuntimeError as e:
        return [Issue(file=".env", message=f"EventKit access denied: {e}")]
    except Exception as e:  # noqa: BLE001
        return [Issue(file=".env", message=f"EventKit probe failed: {type(e).__name__}: {e}")]
    return []


_GARMIN_LAG_LIMIT_MIN = 36 * 60.0
_LISTENER_LAG_LIMIT_MIN = 10.0


def _memory_db_ro() -> sqlite3.Connection | None:
    from src.orchestrator import config

    path = Path(config.MEMORY_DB_PATH)
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _daemon_state_value(key: str) -> str | None:
    conn = _memory_db_ro()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM daemon_state WHERE key=?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _age_minutes(iso_ts: str) -> float | None:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 60.0


_BACKUP_FRESHNESS_LIMIT_MIN = 36 * 60


@register(severity="warn")
def check_backup_freshness(cfg, repo_root: Path) -> list[Issue]:
    if os.getenv("BRAIN_BACKUP_EXPECTED", "").strip() != "1":
        return []

    from src.orchestrator import config

    override = os.getenv("BRAIN_BACKUP_STAMP_PATH", "").strip()
    stamp = (
        Path(override).expanduser()
        if override
        else Path(config.MEMORY_DB_PATH).parent / "backup-success.stamp"
    )
    try:
        info = stamp.lstat()
    except FileNotFoundError:
        return [Issue(
            file=str(stamp),
            message="backup success stamp is missing; no successful backup is recorded",
        )]
    except OSError as exc:
        return [Issue(
            file=str(stamp),
            message=f"backup success stamp cannot be inspected: {type(exc).__name__}",
        )]

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return [Issue(
            file=str(stamp),
            message="backup success stamp must be a real regular file, not a symlink",
        )]
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return [Issue(
            file=str(stamp),
            message="backup success stamp must be owned by the current user",
        )]
    if stat.S_IMODE(info.st_mode) != 0o600:
        return [Issue(
            file=str(stamp),
            message="backup success stamp must have mode 0600",
        )]

    try:
        completed_at = read_regular_text(stamp).strip()
    except (OSError, UnicodeError) as exc:
        return [Issue(
            file=str(stamp),
            message=f"backup success stamp cannot be read safely: {type(exc).__name__}",
        )]
    age = _age_minutes(completed_at)
    if age is None:
        return [Issue(
            file=str(stamp),
            message="backup success stamp does not contain a valid timestamp",
        )]
    if age < -5:
        return [Issue(
            file=str(stamp),
            message="backup success stamp is unexpectedly dated in the future",
        )]
    if age > _BACKUP_FRESHNESS_LIMIT_MIN:
        return [Issue(
            file=str(stamp),
            message=(
                f"backup is stale: last successful completion was {age / 60:.1f} "
                "hours ago (limit 36 hours)"
            ),
        )]
    return []


def _heartbeat_issue(key: str, limit_min: float, what: str) -> list[Issue]:
    value = _daemon_state_value(key)
    if value is None:
        started = _daemon_state_value("daemon_started_at")
        if started is None:
            return []
        start_age = _age_minutes(started)
        if start_age is None:
            return [Issue(
                file="daemon_state",
                message=f"daemon_started_at is not a timestamp: {started!r}",
            )]
        if start_age <= limit_min:
            return []
        return [Issue(
            file="daemon_state",
            message=(
                f"{what} has never reported healthy in {start_age:.0f} min "
                f"since daemon start (grace {limit_min:.0f})"
            ),
        )]
    age = _age_minutes(value)
    if age is None:
        return [Issue(file="daemon_state", message=f"{key} is not a timestamp: {value!r}")]
    if age > limit_min:
        return [Issue(
            file="daemon_state",
            message=(
                f"{what} stale: {key} last stamped {age:.0f} min ago "
                f"(limit {limit_min:.0f})"
            ),
        )]
    return []


@register(severity="warn")
def check_garmin_lag(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator.config import GARMIN_ENABLED

    if not GARMIN_ENABLED:
        return []
    return _heartbeat_issue(
        "last_garmin_sync", _GARMIN_LAG_LIMIT_MIN, "garmin sync (watch metrics)"
    )


@register(severity="error")
def check_garmin_configuration(cfg, repo_root: Path) -> list[Issue]:
    import importlib.util

    from src.orchestrator.config import GARMIN_ENABLED, GARMIN_TOKENSTORE

    if not GARMIN_ENABLED:
        return []
    issues: list[Issue] = []
    if importlib.util.find_spec("garminconnect") is None:
        issues.append(Issue(
            file="pyproject.toml",
            message="Garmin is enabled but garminconnect is absent; install .[garmin]",
        ))
    try:
        store_info = GARMIN_TOKENSTORE.lstat()
    except FileNotFoundError:
        store_info = None
    if store_info is not None and stat.S_ISLNK(store_info.st_mode):
        issues.append(Issue(
            file=str(GARMIN_TOKENSTORE),
            message="Garmin token store path must not be a symlink",
        ))
        return issues
    if store_info is not None and stat.S_ISDIR(store_info.st_mode):
        if hasattr(os, "getuid") and store_info.st_uid != os.getuid():
            issues.append(Issue(
                file=str(GARMIN_TOKENSTORE),
                message="Garmin token directory has the wrong owner",
            ))
        if stat.S_IMODE(store_info.st_mode) != 0o700:
            issues.append(Issue(
                file=str(GARMIN_TOKENSTORE),
                message="Garmin token directory must have mode 0700",
            ))
        token_path = GARMIN_TOKENSTORE / "garmin_tokens.json"
    else:
        token_path = GARMIN_TOKENSTORE
    try:
        token_info = token_path.lstat()
    except FileNotFoundError:
        issues.append(Issue(
            file=str(token_path),
            message="Garmin is enabled but its token store does not exist",
        ))
        return issues
    if stat.S_ISLNK(token_info.st_mode) or not stat.S_ISREG(token_info.st_mode):
        issues.append(Issue(
            file=str(token_path),
            message="Garmin token store must be a real regular file",
        ))
    elif hasattr(os, "getuid") and token_info.st_uid != os.getuid():
        issues.append(Issue(
            file=str(token_path),
            message="Garmin token store has the wrong owner",
        ))
    elif stat.S_IMODE(token_info.st_mode) != 0o600:
        issues.append(Issue(
            file=str(token_path),
            message="Garmin token store must have mode 0600",
        ))
    return issues


@register(severity="warn")
def check_followups_config(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime.loader import load_yaml, resolve_user_dir
    from src.orchestrator.runners.run_guard import feature_on

    if not feature_on("BRAIN_FOLLOWUPS", os.environ):
        return []
    user_dir = resolve_user_dir(repo_root)
    if not user_dir.is_dir() or not any(user_dir.iterdir()):
        return []
    path = user_dir / "followups.yaml"
    if not path.is_file():
        reason = "followups.yaml is missing"
    else:
        try:
            overlay = load_yaml(path)
        except Exception as exc:
            return [Issue(file=str(path), message=f"followups.yaml cannot be read: {exc}")]
        if isinstance(overlay, dict) and any(
            isinstance(overlay.get(key), list) for key in ("exclude_projects", "exclude_paths")
        ):
            return []
        reason = "followups.yaml names neither exclude_projects nor exclude_paths"
    return [Issue(
        file=str(path),
        message=(
            f"{reason}, so the follow-up collector reads every project folder under "
            "~/.claude/projects; list client work under exclude_projects or exclude_paths"
        ),
    )]


@register(severity="warn")
def check_follow_up_scheduled(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator.runners.run_guard import feature_on

    if not feature_on("BRAIN_FOLLOWUPS", os.environ):
        return []
    modes = cfg.modes.values() if cfg is not None else ()
    if any(mode.schedule and "follow_up" in mode.agents() for mode in modes):
        return []
    return [Issue(
        file="user/modes.yaml",
        message=(
            "no scheduled mode runs follow_up, so follow-ups are never collected; list it "
            "in the night mode (shipped in config/modes.yaml)"
        ),
    )]


@register(severity="warn")
def check_slack_listener(cfg, repo_root: Path) -> list[Issue]:
    if cfg is not None:
        parsed = _integration_choice(cfg.integrations.get("notifier", "noop"))
        if parsed is None or parsed[0] != "slack":
            return []
    return _heartbeat_issue(
        "slack_listener_heartbeat", _LISTENER_LAG_LIMIT_MIN, "slack reply listener"
    )


@register(severity="error")
def check_vault_instructions_roots(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime import vault_instructions as vi

    issues: list[Issue] = []
    vic = getattr(cfg, "vault_instructions", None) or {}
    roots = vic.get("allowed_roots") or []
    for agent, paths in (vic.get("agents") or {}).items():
        for rel in paths or []:
            if not vi.path_within_roots(str(rel), roots):
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=(
                            f"instruction path '{rel}' is absolute, uses '..' "
                            "traversal, or escapes allowed_roots"
                        ),
                    )
                )
    return issues


@register(severity="warn")
def check_vault_instructions_files(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime import vault_instructions as vi
    from src.orchestrator import config as _oc

    vault = Path(_oc.VAULT_PATH)
    if not vault.is_dir():
        return []

    issues: list[Issue] = []
    vic = getattr(cfg, "vault_instructions", None) or {}
    roots = vic.get("allowed_roots") or []
    for agent, paths in (vic.get("agents") or {}).items():
        for rel in paths or []:
            rel = str(rel)
            if not vi.path_within_roots(rel, roots):
                continue
            try:
                with vi.open_instruction_file(vault, rel) as handle:
                    size = os.fstat(handle.fileno()).st_size
            except FileNotFoundError:
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=f"instruction file not found in vault: {rel}",
                    )
                )
                continue
            except vi.UnsafeInstructionPathError as e:
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=f"instruction file has an unsafe path: {rel}: {e}",
                    )
                )
                continue
            except OSError as e:
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=f"instruction file unreadable: {rel}: {e}",
                    )
                )
                continue
            if size > vi.CAP_BYTES:
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=(
                            f"instruction file {rel} is {size} bytes, over the "
                            f"{vi.CAP_BYTES}-byte cap (it will be truncated at render)"
                        ),
                    )
                )
    return issues


_CLAUDE_IMPORT_RE = re.compile(r"^@([\w./-]+\.md)[ \t]*$", re.MULTILINE)


@register(severity="error")
def check_vault_instructions_imports(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime import vault_instructions as vi
    from src.orchestrator import config as _oc

    vault = Path(_oc.VAULT_PATH)
    if not vault.is_dir():
        return []

    issues: list[Issue] = []
    vic = getattr(cfg, "vault_instructions", None) or {}
    roots = vic.get("allowed_roots") or []
    for agent, paths in (vic.get("agents") or {}).items():
        for rel in paths or []:
            rel = str(rel)
            if not vi.path_within_roots(rel, roots):
                continue
            try:
                with vi.open_instruction_file(vault, rel) as handle:
                    body = handle.read(vi.CAP_BYTES).decode("utf-8", "replace")
            except (FileNotFoundError, vi.UnsafeInstructionPathError, OSError):
                continue
            for target in _CLAUDE_IMPORT_RE.findall(body):
                issues.append(
                    Issue(
                        file=f"vault_instructions::{agent}",
                        message=(
                            f"instruction file {rel} imports '@{target}' with "
                            "Claude Code '@' syntax, which the agent runner does "
                            f"not expand. Allowlist {target} directly "
                            "instead."
                        ),
                    )
                )
    return issues


@register(severity="warn")
def check_memory_db_integrity(cfg, repo_root: Path) -> list[Issue]:
    from src.orchestrator import config

    path = Path(config.MEMORY_DB_PATH)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [Issue(file=str(path), message=f"memory DB unreadable: {e}")]
    verdict = row[0] if row else "no output"
    if verdict != "ok":
        return [Issue(
            file=str(path),
            message=f"PRAGMA quick_check failed: {verdict} — restore from backup "
                    "(scripts/backup_memory.sh snapshots nightly)",
        )]
    return []


_MEDIA_CONFIG = "config/media.yaml"
_MODEL_HASH = re.compile(r"[0-9a-f]{64}")
_MODEL_HASH_UNPINNED = "TO-BE-PINNED"


@register(severity="warn")
def check_whisper_model(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.media.voice import model_path
    from src.orchestrator.runners.run_guard import feature_on

    if not feature_on("BRAIN_MEDIA", os.environ):
        return []
    model = model_path(os.environ)
    if model is None:
        if os.getenv("BRAIN_DOCTOR_SKIP_RUNTIME") == "1":
            return []
        return [Issue(
            file=".env",
            message=(
                "BRAIN_WHISPER_MODEL is not set while BRAIN_MEDIA is on; voice notes get the "
                "type-it reply until the model is configured"
            ),
        )]
    if not model.is_file():
        return [Issue(
            file=".env",
            message=f"BRAIN_WHISPER_MODEL {model} is not a file; a voice note gets the 'please type it' reply",
        )]
    try:
        data = yaml.safe_load(read_regular_text(repo_root / _MEDIA_CONFIG)) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [Issue(file=_MEDIA_CONFIG, message=f"{_MEDIA_CONFIG} cannot be read ({type(exc).__name__})")]
    whisper = data.get("whisper") if isinstance(data, dict) else None
    pinned = str(whisper.get("sha256") or "").strip() if isinstance(whisper, dict) else ""
    if pinned == _MODEL_HASH_UNPINNED:
        return [Issue(
            file=_MEDIA_CONFIG,
            message=(
                f"the Whisper model hash is not pinned (whisper.sha256 is {_MODEL_HASH_UNPINNED}); "
                "set it to the SHA-256 of the model file"
            ),
        )]
    if not _MODEL_HASH.fullmatch(pinned.lower()):
        return [Issue(
            file=_MEDIA_CONFIG,
            message=f"whisper.sha256 must be 64 hex digits or {_MODEL_HASH_UNPINNED}",
        )]
    try:
        with model.open("rb") as handle:
            found = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        return [Issue(file=".env", message=f"BRAIN_WHISPER_MODEL {model} cannot be read ({type(exc).__name__})")]
    if found != pinned.lower():
        return [Issue(
            file=_MEDIA_CONFIG,
            message=(
                f"{model.name} does not match the pinned hash "
                f"(expected {pinned.lower()[:12]}..., found {found[:12]}...)"
            ),
        )]
    return []


@register(severity="warn")
def check_media_dir_mode(cfg, repo_root: Path) -> list[Issue]:
    from src.brain.runtime.loader import resolve_user_dir

    folder = resolve_user_dir(repo_root) / "media"
    try:
        info = folder.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [Issue(file=str(folder), message="the media folder must be a real directory")]
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        return [Issue(file=str(folder), message=f"the media folder must have mode 0700, not {mode:04o}")]
    return []


_CONNECTOR_EXAMPLE = "examples/vault_connector.example.yaml"


def _connector_file() -> Path:
    from src.brain.connector.config import DEFAULT_CONFIG_PATH, user_dir

    return user_dir() / DEFAULT_CONFIG_PATH


def _connector_config():
    """The connector settings and the issues found reading them."""
    from src.brain.connector.config import load_config

    path = _connector_file()
    if not path.is_file():
        if os.getenv("BRAIN_DOCTOR_SKIP_RUNTIME") == "1":
            return None, []
        return None, [Issue(
            file=str(path),
            message=(
                "the vault connector is not set up; to use it, copy "
                f"{_CONNECTOR_EXAMPLE} to user/vault_connector.yaml and fill it in"
            ),
            severity="info",
        )]
    try:
        return load_config(path), []
    except (ValueError, OSError, yaml.YAMLError) as exc:
        return None, [Issue(file=str(path), message=str(exc))]


def _never_crash(fn):
    """Turn any exception in a connector check into one warning.

    The connector is its own launchd service, so a broken connector install must
    never stop the daemon.
    """

    @functools.wraps(fn)
    def wrapper(cfg, repo_root: Path) -> list[Issue]:
        try:
            return fn(cfg, repo_root)
        except Exception as exc:  # noqa: BLE001
            return [Issue(
                file="<doctor>",
                message=f"{fn.__name__} crashed: {type(exc).__name__}: {exc}",
                severity="warn",
            )]

    return wrapper


@register(severity="warn", connector=True)
@_never_crash
def check_connector_config(cfg, repo_root: Path) -> list[Issue]:
    config, issues = _connector_config()
    if config is None:
        return issues
    where = str(_connector_file())
    problems: list[str] = []
    if not config.vault_path.is_dir():
        problems.append("vault_path does not exist or is not a folder")
    vault = config.vault_path.resolve()
    data = config.data_dir.resolve()
    if data.is_relative_to(vault):
        problems.append("data_dir must not be inside vault_path")
    elif vault.is_relative_to(data):
        problems.append("vault_path must not be inside data_dir")
    if not config.public_url.startswith("https://"):
        problems.append("public_url must start with https://")
    return [Issue(file=where, message=problem) for problem in problems]


@register(severity="warn", connector=True)
@_never_crash
def check_connector_sign_in(cfg, repo_root: Path) -> list[Issue]:
    config, _ = _connector_config()
    if config is None:
        return []
    from src.brain.connector.auth.store import HASH_SETTING, TOTP_SETTING

    database = config.data_dir / "auth.db"
    found: set[str] = set()
    try:
        if database.is_file():
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as conn:
                rows = conn.execute(
                    "SELECT key FROM settings WHERE key IN (?, ?)", (HASH_SETTING, TOTP_SETTING)
                ).fetchall()
            found = {row[0] for row in rows}
    except sqlite3.Error as exc:
        if "no such table" not in str(exc):
            return [Issue(file=str(database), message=f"cannot read auth.db: {exc}")]
    steps = []
    if HASH_SETTING not in found:
        steps.append("python -m src.brain.connector set-password")
    if TOTP_SETTING not in found:
        steps.append("python -m src.brain.connector totp-setup")
    if not steps:
        return []
    return [Issue(
        file=str(database),
        message="connector sign-in is not set up; run " + " and ".join(steps),
    )]


@register(severity="warn", connector=True)
@_never_crash
def check_connector_instructions(cfg, repo_root: Path) -> list[Issue]:
    config, _ = _connector_config()
    if config is None:
        return []
    from src.brain.connector.instructions import load_overlay, render_instructions

    try:
        overlay = load_overlay(config.instructions_overlay, config.profile_path)
        render_instructions(overlay, config.layout, config.integrations)
    except Exception as exc:  # noqa: BLE001
        return [Issue(
            file=str(config.instructions_overlay),
            message=f"the server instructions do not render: {exc}",
        )]
    return []


@register(severity="warn", connector=True)
@_never_crash
def check_connector_integrations(cfg, repo_root: Path) -> list[Issue]:
    config, _ = _connector_config()
    if config is None:
        return []
    enabled = config.integrations.enabled_names()
    where = str(_connector_file())
    result = [Issue(
        file=where,
        message=(
            "connector integrations on: " + (", ".join(enabled) or "none")
            + f"; time zone {config.timezone} (from {config.timezone_source})"
        ),
        severity="info",
    )]
    if {"calendar", "mail", "contacts"} & set(enabled):
        apple_id, app_password = _icloud_creds()
        if not (apple_id and app_password):
            names = ", ".join(n for n in enabled if n != "reminders")
            result.append(Issue(
                file=where,
                message=(
                    f"{names} need an iCloud sign-in: set ICLOUD_APPLE_ID in .env, "
                    "and the app-specific password as ICLOUD_APP_PASSWORD in .env or "
                    "in the Keychain item brain-icloud-app-password (see docs/INTEGRATIONS.md)"
                ),
            ))
    if "reminders" in enabled:
        if sys.platform != "darwin":
            result.append(Issue(file=where, message="reminders needs macOS"))
        else:
            from src.brain.mcp.tools import _icloud

            if not _icloud.reminders_access_granted():
                result.append(Issue(
                    file=where,
                    message=(
                        "this process has no Reminders access; macOS grants it to the "
                        "connector's launchd job on its own, so check it from claude.ai "
                        "(docs/vault-connector-setup.md, Reminders access)"
                    ),
                    severity="info",
                ))
    return result


@register(severity="warn", connector=True)
@_never_crash
def check_connector_daily_note_template(cfg, repo_root: Path) -> list[Issue]:
    config, _ = _connector_config()
    if config is None or not config.vault_path.is_dir():
        return []
    template = config.vault_path / config.layout.template_path
    if template.is_file():
        return []
    return [Issue(
        file=str(template),
        message=(
            f"the daily note template {config.layout.template_path} is missing in the "
            f"vault, so appending to today's note fails; run `python -m src.cli init-vault "
            f"{config.vault_path}` (it never overwrites a file) or create the note"
        ),
    )]
