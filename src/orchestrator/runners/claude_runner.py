from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    AgentDefinition,
    HookContext,
    HookInput,
    HookJSONOutput,
    McpSdkServerConfig,
    SyncHookJSONOutput,
)

from src.brain.mcp.tools.web import query_scope_denial
from src.brain.net_security import public_query_terms
from src.brain.runtime.edit_backups import keep_copy
from src.db import record_event, record_vault_edit
from src.orchestrator.config import REPLY_AGENT_TIMEOUT, VAULT_PATH
from src.orchestrator.runners.base import (
    AgentRunResult,
    ClaudeRuntimeError,
    resolve_agent_name,
    safe_insert_agent_run,
    telemetry_agent_name,
)
from src.orchestrator.runners.brain_tool_bridge import (
    REPO_ROOT,
    SERVER_NAME,
    ApprovalTracker,
    ReplyDeliveryTracker,
    build_brain_server,
    load_brain_tool_specs,
    require_known_brain_tools,
)
from src.orchestrator.runners.helpers import helper_allowed_tools, helper_definition
from src.orchestrator.runners.run_guard import (
    BRAIN_TOOL_PREFIX,
    HelperCap,
    OutsideContentState,
    WebRiskBarrier,
    _tool_target,
    bare_tool_name,
    confinement_denial,
    edit_denial,
    feature_denial,
    feature_on,
    helper_tool_denial,
    scrubbed_env,
    vault_offlimits,
    verify_denial,
)

log = logging.getLogger("brain.runners.claude")


def _owner_place_terms() -> frozenset[str]:
    from src.brain.runtime.places import configured_places

    return public_query_terms(" ".join(configured_places(Path(__file__).resolve().parents[3])))

CLAUDE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"

SUPPORTED_BUILTINS = ("Read", "Glob", "Grep", "Edit", "Write", "Agent")

# Claude Code refuses a call to a disallowed tool even when the session still offers it.
DENIED_TOOLS: tuple[str, ...] = (
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Agent", "Task",
    "WebSearch", "WebFetch", "Skill",
)

# `Task` is the name the Claude Code CLI uses for `Agent`.
STRICT_BUILTINS = ("Edit", "Write", "Agent", "Task")

EDIT_TOOLS = ("Edit", "Write")
# The Claude Code CLI offers the helper tool as `Task`; frontmatter grants it as `Agent`.
HELPER_START_TOOLS = ("Agent", "Task")

MAX_HELPERS = 3

DEFAULT_DAILY_NOTES_DIR = "01 - Daily Notes"

# Claude Code switches that keep loaded user content out of a session even with
# no settings files: no auto-memory (the vault path has its own MEMORY.md),
# no organisation memory, and no AGENTS.md or CLAUDE.md attached from the
# folders above a file the agent reads.
ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
}

VERIFY_MARKER = ".brain-verify-copy"

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _slack_value(value: object) -> str | None:
    """A Slack channel id or timestamp the tools may use, or None."""
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9.:_-]{1,64}", text) else None


def split_tools(agent_cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    builtins: list[str] = []
    brain_tools: list[str] = []
    for tool in agent_cfg.get("tools") or []:
        if tool.startswith(BRAIN_TOOL_PREFIX):
            brain_tools.append(tool.removeprefix(BRAIN_TOOL_PREFIX))
        elif tool in SUPPORTED_BUILTINS:
            builtins.append(tool)
        else:
            raise ClaudeRuntimeError(f"unsupported tool in agent config: {tool}")
    return builtins, brain_tools


def denied_for(builtins: Sequence[str]) -> list[str]:
    """The tools to disallow for a run: all of `DENIED_TOOLS` except the granted built-ins.

    The CLI reads a rule for `Task` or `Agent` as a rule for both, and a deny beats an allow.
    """
    granted = set(builtins)
    if "Agent" in granted:
        granted.add("Task")
    return [tool for tool in DENIED_TOOLS if tool not in granted]


def model_for_agent(agent_cfg: dict[str, Any]) -> str:
    configured = agent_cfg.get("model") or DEFAULT_MODEL
    if configured not in CLAUDE_MODELS:
        raise ClaudeRuntimeError(
            f"model {configured} is not one of {', '.join(CLAUDE_MODELS)}",
        )
    return str(configured)


def _grants(
    agent_cfg: dict[str, Any], environ: Mapping[str, str],
) -> tuple[list[str], list[str], int]:
    builtins, brain_tools = split_tools(agent_cfg)
    brain_tools = [tool for tool in brain_tools if feature_denial(tool, environ) is None]
    if not feature_on("BRAIN_DIRECT_EDITS", environ):
        builtins = [tool for tool in builtins if tool not in EDIT_TOOLS]
    max_helpers = 0
    if "Agent" in builtins and feature_on("BRAIN_HELPERS", environ):
        max_helpers = min(MAX_HELPERS, max(0, int(agent_cfg.get("max_helpers") or 0)))
    if not max_helpers:
        builtins = [tool for tool in builtins if tool != "Agent"]
    return builtins, brain_tools, max_helpers


def _vault_offlimits() -> list[str]:
    from src.brain.runtime.loader import load_all

    cfg = load_all(REPO_ROOT)
    daily = (cfg.profile.get("notes") or {}).get("daily_notes_dir") or DEFAULT_DAILY_NOTES_DIR
    return vault_offlimits(cfg.vault_instructions, daily)


def check_subscription_auth(env: Mapping[str, str]) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if (env.get(name) or "").strip():
            raise ClaudeRuntimeError(
                f"{name} is set; agents run on the Claude subscription only. "
                "Remove it from the environment and from .env.",
            )


def check_verify_run(env: Mapping[str, str]) -> bool:
    """True for a safe verification run, False when off; raises when on but unsafe."""
    if not _truthy(env.get("BRAIN_VERIFY_RUN")):
        return False
    if not _truthy(env.get("BRAIN_SLACK_OFF")):
        raise ClaudeRuntimeError("a verification run needs BRAIN_SLACK_OFF=1")
    db_path = (env.get("MEMORY_DB_PATH") or "").strip()
    if not db_path or not (Path(db_path).expanduser().parent / VERIFY_MARKER).is_file():
        raise ClaudeRuntimeError(
            f"a verification run needs MEMORY_DB_PATH in a folder holding {VERIFY_MARKER}",
        )
    vault = (env.get("VAULT_PATH") or "").strip()
    if not vault or not (Path(vault).expanduser() / VERIFY_MARKER).is_file():
        raise ClaudeRuntimeError(
            f"a verification run needs VAULT_PATH pointing at a folder holding {VERIFY_MARKER}",
        )
    user_dir = (env.get("USER_DIR") or "").strip()
    if not user_dir or not (Path(user_dir).expanduser() / VERIFY_MARKER).is_file():
        raise ClaudeRuntimeError(
            f"a verification run needs USER_DIR pointing at a copy of the overlay holding {VERIFY_MARKER}",
        )
    return True


def build_options(
    *,
    system_prompt: str,
    model: str,
    effort: str,
    max_turns: int,
    builtins: list[str],
    brain_tool_names: list[str],
    brain_server: McpSdkServerConfig | None,
    hooks: dict[Any, Any],
    cwd: Path,
    denied: list[str],
    agents: dict[str, AgentDefinition] | None,
    env: dict[str, str],
) -> ClaudeAgentOptions:
    allowed = [*builtins, *(f"{BRAIN_TOOL_PREFIX}{name}" for name in brain_tool_names)]
    servers: dict[str, Any] = {}
    if brain_tool_names and brain_server is not None:
        servers[SERVER_NAME] = brain_server
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        effort=effort,  # type: ignore[arg-type]
        max_turns=max_turns,
        cwd=str(cwd),
        tools=list(builtins),
        allowed_tools=allowed,
        disallowed_tools=denied,
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers=servers,
        agents=agents,
        skills=[],
        hooks=hooks,
        env={**ISOLATION_ENV, **env},
        extra_args={"no-session-persistence": None},
    )


def check_session_tools(
    init_data: Mapping[str, Any], allowed: set[str], denied: set[str],
) -> str | None:
    """Reason to stop a session that offers anything beyond what was granted.

    A denied tool may be offered, except one of `STRICT_BUILTINS` without a grant.
    `Agent` and `Task` are one tool, so a grant of either covers both names.
    """
    granted = set(allowed)
    if granted & set(HELPER_START_TOOLS):
        granted |= set(HELPER_START_TOOLS)
    offered = [str(t) for t in init_data.get("tools") or []]
    extra = [
        t for t in offered
        if t not in granted and (t not in denied or t in STRICT_BUILTINS)
    ]
    if extra:
        return f"session offered tools that were not granted: {', '.join(extra)}"
    servers = [
        str(s.get("name")) if isinstance(s, Mapping) else str(s)
        for s in init_data.get("mcp_servers") or []
    ]
    unexpected = [s for s in servers if s != SERVER_NAME]
    if unexpected:
        return f"session loaded MCP servers that were not granted: {', '.join(unexpected)}"
    return None


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class UsageLimit:
    FALLBACK_SECONDS = 3600.0

    def __init__(self) -> None:
        self.blocked_until: float | None = None

    def active(self, now: float) -> bool:
        return self.blocked_until is not None and now < self.blocked_until

    def block(self, resets_at: float | None, now: float) -> bool:
        first = not self.active(now)
        until = resets_at if resets_at and resets_at > now else now + self.FALLBACK_SECONDS
        if self.blocked_until is None or first or until > self.blocked_until:
            self.blocked_until = until
        return first

    def _resume_time(self) -> str:
        return datetime.fromtimestamp(self.blocked_until or time.time()).strftime("%H:%M")

    def error(self) -> str:
        return f"usage limit reached (resumes after {self._resume_time()})"

    def notice(self) -> str:
        return f"Claude usage limit reached. Agent runs resume after {self._resume_time()}."


USAGE_LIMIT = UsageLimit()

_RUN_LOCKS: dict[tuple[int, str], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _run_lock(kind: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), kind)
    entry = _RUN_LOCKS.get(key)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _RUN_LOCKS[key] = entry
    return entry[1]


async def _notify_slack(text: str) -> None:
    try:
        from src.brain.mcp._common import notifier
        await notifier().send(text, title="", urgency="info", area=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("usage-limit notice failed: %s", exc)


async def _one_user_turn(text: str, images: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": str(image.get("mimeType") or "image/png"),
                "data": str(image.get("data") or ""),
            },
        }
        for image in images
    ]
    content.append({"type": "text", "text": text})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _deny(reason: str) -> SyncHookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _edit_target(vault_root: Path, tool_input: Mapping[str, Any]) -> tuple[Path, str]:
    """Claude Code reads the path trimmed, and a relative one against its working
    directory, the vault.
    """
    given = str(tool_input.get("file_path") or "")
    root = Path(os.path.abspath(vault_root))
    target = _tool_target(given, root) or Path(os.path.abspath(root / given))
    if target.is_relative_to(root):
        return target, target.relative_to(root).as_posix()
    return target, given


def _build_hooks(
    *, agent: str, run_label: str, vault_root: Path, read_roots: Sequence[Path],
    edit_paths: Sequence[str], offlimits: Sequence[str], helper_allowed: set[str],
    barrier: WebRiskBarrier, state: OutsideContentState, cap: HelperCap, verify: bool,
    environ: Mapping[str, str],
) -> dict[Any, Any]:
    before_edit: dict[str, str | None] = {}

    def journal(action: str, subject: str, detail: str = "") -> None:
        try:
            record_event(actor=agent, action=action, subject=subject, detail=detail)
        except Exception as exc:
            log.warning("event %s not recorded: %s", action, exc)

    def call_id(data: Mapping[str, Any], tool_use_id: str | None) -> str:
        return tool_use_id or str(data.get("tool_use_id") or "")

    async def pre_tool_use(input_data: HookInput, tool_use_id: str | None,
                           _context: HookContext) -> HookJSONOutput:
        data: Mapping[str, Any] = input_data
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        bare = bare_tool_name(tool_name)
        reason = (
            confinement_denial(tool_name, tool_input, read_roots, cwd=vault_root)
            or helper_tool_denial(data, helper_allowed)
            or cap.check(tool_name, tool_input)
            or (
                query_scope_denial(tool_input, barrier.query_terms, private=barrier.private)
                if bare == "web_search" else None
            )
            or barrier.check(bare)
            or (verify_denial(bare) if verify else None)
            or feature_denial(bare, environ)
        )
        if reason:
            journal("tool_refused", bare, reason[:300])
            return _deny(reason)
        if tool_name in EDIT_TOOLS:
            reason = edit_denial(
                tool_name, tool_input, vault_root=vault_root,
                edit_paths=edit_paths, offlimits=offlimits, state=state,
            )
            target, shown = _edit_target(vault_root, tool_input)
            if reason:
                journal("edit_refused", shown, reason)
                return _deny(reason)
            try:
                keep_copy(target, shown)
            except OSError as exc:
                reason = f"The previous version could not be saved ({type(exc).__name__}), so the edit was refused"
                journal("edit_refused", shown, reason)
                return _deny(reason)
            before_edit[call_id(data, tool_use_id)] = _sha256_of(target)
        state.note_tool(bare)
        if tool_name in HELPER_START_TOOLS:
            journal("helper_start", run_label)
        return {}

    async def post_tool_use(input_data: HookInput, tool_use_id: str | None,
                            _context: HookContext) -> HookJSONOutput:
        data: Mapping[str, Any] = input_data
        tool_name = str(data.get("tool_name") or "")
        if tool_name not in EDIT_TOOLS:
            return {}
        target, shown = _edit_target(vault_root, data.get("tool_input") or {})
        sha_before = before_edit.pop(call_id(data, tool_use_id), None)
        try:
            record_vault_edit(
                agent=agent, run_label=run_label, tool=tool_name, path=shown,
                sha_before=sha_before, sha_after=_sha256_of(target),
            )
        except Exception as exc:
            log.error("direct edit of %s is missing from the ledger: %s", shown, exc)
        journal("direct_edit", shown)
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher="Edit|Write", hooks=[post_tool_use])],
    }


QueryFn = Callable[..., AsyncIterator[Any]]


class ClaudeRunner:
    name = "claude"

    def __init__(
        self,
        query_fn: QueryFn | None = None,
        notify_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._query: QueryFn = query_fn or claude_agent_sdk.query
        self._notify = notify_fn or _notify_slack

    async def run(self, agent_cfg: dict[str, Any], trigger: str,
                  context: dict[str, Any]) -> AgentRunResult:
        return await self._invoke(agent_cfg, trigger, context, is_reply=False)

    async def run_reply(self, text: str, agent_cfg: dict[str, Any],
                        context: dict[str, Any]) -> AgentRunResult:
        return await self._invoke(agent_cfg, text, context, is_reply=True)

    async def _invoke(self, agent_cfg: dict[str, Any], prompt: str,
                      context: dict[str, Any], *, is_reply: bool) -> AgentRunResult:
        agent_name = resolve_agent_name(context, agent_cfg)
        telemetry_name = telemetry_agent_name(agent_name, is_reply)
        started = datetime.now(UTC)
        start = time.monotonic()
        model = str(agent_cfg.get("model") or DEFAULT_MODEL)

        def record(status: str, error: str | None = None, **extra: Any) -> None:
            safe_insert_agent_run(
                agent=telemetry_name, provider=self.name, model=model, status=status,
                duration_ms=int((time.monotonic() - start) * 1000),
                started_at=_utc_iso(started), completed_at=_utc_iso(datetime.now(UTC)),
                error=error or "", **extra,
            )

        try:
            system_prompt = context.get("system_prompt")
            if not isinstance(system_prompt, str) or not system_prompt.strip():
                raise ClaudeRuntimeError("a Claude run needs a rendered system_prompt")
            check_subscription_auth(os.environ)
            verify = check_verify_run(os.environ)
            model = model_for_agent(agent_cfg)
            builtins, brain_tools, max_helpers = _grants(agent_cfg, os.environ)
            caller_context = self._caller_context(
                agent_cfg, agent_name, prompt, context, is_reply=is_reply,
            )
            specs: list[dict[str, Any]] = []
            if brain_tools:
                specs = await load_brain_tool_specs()
                require_known_brain_tools(brain_tools, specs)
            can_edit = any(tool in builtins for tool in EDIT_TOOLS)
            edit_paths = list(agent_cfg.get("vault_paths") or []) if can_edit else []
            offlimits = _vault_offlimits() if can_edit else []
        except (ClaudeRuntimeError, ValueError) as exc:
            record("failed", str(exc))
            raise ClaudeRuntimeError(str(exc)) from exc

        images = list((context.get("msg") or {}).get("images") or [])
        barrier = WebRiskBarrier(caller_context)
        state = OutsideContentState(
            caller_context, unattended=not is_reply, seen=caller_context["outside_content"],
        )
        approvals = ApprovalTracker()
        replies = ReplyDeliveryTracker()
        server = (
            build_brain_server(
                specs, brain_tools, caller_context,
                barrier=barrier, approvals=approvals, replies=replies, verify=verify,
                state=state,
            )
            if brain_tools else None
        )
        agents = {"helper": helper_definition(brain_tools)} if "Agent" in builtins else None
        vault_root = Path(VAULT_PATH)
        read_roots = context.get("read_roots")
        hooks = _build_hooks(
            agent=agent_name,
            run_label=f"{telemetry_name}@{started.isoformat()}",
            vault_root=vault_root,
            read_roots=[vault_root] if read_roots is None else [Path(r) for r in read_roots],
            edit_paths=edit_paths,
            offlimits=offlimits,
            helper_allowed=helper_allowed_tools(agents["helper"]) if agents else set(),
            barrier=barrier, state=state, cap=HelperCap(max_helpers), verify=verify,
            environ=dict(os.environ),
        )

        options = build_options(
            system_prompt=f"{system_prompt}\n\nYou are running as `{model}`.",
            model=model,
            effort=str(agent_cfg.get("effort") or DEFAULT_EFFORT),
            max_turns=int(agent_cfg.get("max_turns") or 30),
            builtins=builtins,
            brain_tool_names=brain_tools,
            brain_server=server,
            hooks=hooks,
            cwd=vault_root,
            denied=denied_for(builtins),
            agents=agents,
            env=scrubbed_env(os.environ),
        )
        timeout = float(context.get("timeout") or REPLY_AGENT_TIMEOUT)

        async with _run_lock("reply" if is_reply else "run"):
            if USAGE_LIMIT.active(time.time()):
                record("skipped", USAGE_LIMIT.error())
                return self._skipped(agent_name, model, USAGE_LIMIT.error())
            try:
                outcome = await asyncio.wait_for(
                    self._consume(_one_user_turn(prompt, images), options), timeout=timeout,
                )
            except TimeoutError as exc:
                error = f"Claude exceeded {timeout:.0f}s"
                record("timeout", error)
                raise ClaudeRuntimeError(error) from exc
            except asyncio.CancelledError:
                record("cancelled", "Claude run cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                error = f"Claude run failed: {exc}"
                record("failed", error)
                raise ClaudeRuntimeError(error) from exc

        if outcome["rate_limited"]:
            if USAGE_LIMIT.block(outcome["resets_at"], time.time()):
                await self._notify(USAGE_LIMIT.notice())
            outcome["error"] = USAGE_LIMIT.error()

        return self._finish(
            outcome, agent_name, model, is_reply, replies.delivered,
            time.monotonic() - start, record,
        )

    def _skipped(self, agent_name: str, model: str, error: str) -> AgentRunResult:
        return {
            "agent": agent_name, "provider": self.name, "model": model,
            "status": "failed", "error": error, "response": "",
            "elapsed_s": 0.0, "duration_ms": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": 0.0,
            "tools": 0, "turns": 0, "stop_reason": "", "turn_limit_reached": False,
            "reply_delivered": False,
        }

    @staticmethod
    def _caller_context(agent_cfg: dict[str, Any], agent_name: str, prompt: str,
                        context: dict[str, Any], *, is_reply: bool) -> dict[str, Any]:
        reply_context = context.get("ctx") or {}
        message_context = context.get("msg") or {}
        outside = bool(message_context.get("images")) or message_context.get("outside_content") is True
        return {
            "agent": agent_name,
            "area": context.get("area") or reply_context.get("area") or agent_name,
            "allowed_action_types": list(
                dict.fromkeys(agent_cfg.get("allowed_action_types") or []),
            ),
            "direct_action_types": [],
            "is_reply": is_reply,
            "untrusted_input": bool(
                outside
                or message_context.get("quoted_text")
                or message_context.get("quoted_timestamp")
                or reply_context.get("finding_title")
                or reply_context.get("finding_detail")
            ),
            "web_query_terms": (
                sorted(public_query_terms(prompt[:2000]) | _owner_place_terms()) if is_reply else []
            ),
            "outside_content": outside,
            "areas": context.get("areas"),
            "vault_paths": context.get("vault_paths"),
            "slack_channel": _slack_value(message_context.get("group_id")) if is_reply else None,
            "slack_thread_ts": (
                _slack_value(message_context.get("reply_thread_ts") or message_context.get("timestamp"))
                if is_reply else None
            ),
            "slack_sender": _slack_value(message_context.get("sender")) if is_reply else None,
        }

    async def _consume(self, prompt: AsyncIterator[dict[str, Any]],
                       options: ClaudeAgentOptions) -> dict[str, Any]:
        outcome: dict[str, Any] = {
            "last_text": "", "tools": 0, "findings": 0, "result": None, "error": None,
            "rate_limited": False, "resets_at": None,
        }
        allowed = set(options.allowed_tools)
        denied = set(options.disallowed_tools)
        stream = self._query(prompt=prompt, options=options)
        try:
            async for message in stream:
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    reason = check_session_tools(message.data, allowed, denied)
                    if reason:
                        outcome["error"] = reason
                        break
                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    if info.status == "rejected":
                        outcome["rate_limited"] = True
                        outcome["resets_at"] = float(info.resets_at) if info.resets_at else None
                elif isinstance(message, AssistantMessage):
                    if message.error == "rate_limit":
                        outcome["rate_limited"] = True
                    texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                    if any(t.strip() for t in texts):
                        outcome["last_text"] = "\n".join(t for t in texts if t.strip())
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            outcome["tools"] += 1
                            if bare_tool_name(block.name) == "store_finding":
                                outcome["findings"] += 1
                elif isinstance(message, ResultMessage):
                    outcome["result"] = message
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
        return outcome

    def _finish(self, outcome: dict[str, Any], agent_name: str, model: str, is_reply: bool,
                reply_delivered: bool, elapsed: float,
                record: Callable[..., None]) -> AgentRunResult:
        result: ResultMessage | None = outcome["result"]
        usage = (result.usage if result else None) or {}
        subtype = result.subtype if result else ""
        stop_reason = (result.stop_reason if result else None) or ""
        response = ((result.result if result else None) or outcome["last_text"] or "").strip()
        turn_limit_reached = subtype == "error_max_turns"
        error: str | None = outcome["error"]
        if error is None and result is None:
            error = "Claude returned no result"
        if error is None and result is not None and result.is_error and not turn_limit_reached:
            error = (result.result or subtype or "Claude reported an error").strip()

        if error:
            status = "failed"
        elif reply_delivered:
            status = "success"
        elif turn_limit_reached or stop_reason == "max_tokens":
            status = "partial"
        elif response or not is_reply:
            status = "success"
        else:
            status = "failed"
            error = "Claude completed without a text response"

        run: AgentRunResult = {
            "agent": agent_name,
            "provider": self.name,
            "model": model,
            "status": status,
            "error": error,
            "response": response,
            "elapsed_s": elapsed,
            "duration_ms": int(elapsed * 1000),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cost_usd": float((result.total_cost_usd if result else 0) or 0),
            "tools": int(outcome["tools"]),
            "turns": int(result.num_turns if result else 0),
            "stop_reason": stop_reason,
            "turn_limit_reached": turn_limit_reached,
            "reply_delivered": reply_delivered,
        }
        record(
            status, error,
            cost_usd=run["cost_usd"],
            input_tokens=run["input_tokens"], output_tokens=run["output_tokens"],
            cache_read_tokens=run["cache_read_tokens"],
            cache_creation_tokens=run["cache_creation_tokens"],
            findings_count=int(outcome["findings"]),
        )
        return run
