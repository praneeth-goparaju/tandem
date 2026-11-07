"""The vault connector's web app: two MCP endpoints, sign-in and a health check.

``build_app`` puts together, in one Starlette app:

- ``/mcp``, the full endpoint, and ``/mcp/limited``, the limited one, each an
  ``MCPServer`` with its own tools from ``build_tools``. Both use the same OAuth
  provider, and each accepts only tokens issued for its own address, so a
  limited token gets 401 at ``/mcp`` and the other way round;
- the OAuth routes (``/authorize``, ``/token``, ``/register``, ``/revoke`` and
  the metadata under ``/.well-known/``) and the sign-in page ``/login``;
- ``GET /health``, which answers ``ok`` without a token.

Every tool call is written to the audit log with a short id of the token
(the first 12 hex characters of its SHA-256, never the token), also when the
call is cancelled: a call runs to its end once it has started, so a cancel
never lets a second write start while the first is still writing.

An edit is held for the owner's approval (Claude asks for it with an
Approve/Skip question card) when, in the last 30 minutes,
``mail_read`` or ``mail_search`` ran with the same token or in the same
library session. The token counts too because the 2026-07-28 protocol has no
sessions, so a mail read holds edits in every chat signed in with that token.

The connector listens on 127.0.0.1 behind cloudflared, so every request comes
from a loopback address; ``connector_address`` takes the client's address from
Cloudflare's ``CF-Connecting-IP`` header for the sign-in throttle.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import ipaddress
import json
import logging
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import anyio
import anyio.to_thread
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Route

from src.brain.atomic_io import atomic_write_text
from src.brain.connector.audit import AuditLog
from src.brain.connector.auth.login import login_routes
from src.brain.connector.auth.provider import VaultOAuthProvider
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.auth.throttle import Throttle
from src.brain.connector.config import ConnectorConfig
from src.brain.connector.instructions import load_overlay, render_instructions
from src.brain.connector.integrations import build_tools
from src.brain.connector.integrations.apple import icloud_bridge
from src.brain.connector.tools import (
    HELD_PREFIX,
    HELD_RULE,
    Call,
    ICloudCall,
    ToolDef,
    ToolEnv,
    audit_args,
    audit_target,
    run_tool,
)
from src.brain.connector.vault.undo_store import UndoStore
from src.orchestrator import config as orchestrator_config

_logger = logging.getLogger(__name__)

AUTH_DB = "auth.db"
UNDO_DB = "undo.db"
AUDIT_DIR = "audit"
INSTRUCTIONS_FILE = "instructions.md"
ALERT_TITLE = "Repeated failed sign-ins to the vault connector"
MAIL_HOLD = timedelta(minutes=30)
MAIL_READ_TOOLS = frozenset({"mail_read", "mail_search"})
CLAUDE_ORIGINS = ("https://claude.ai", "https://claude.com")


def connector_address(request: Request) -> str:
    """The sign-in client's address.

    Behind cloudflared the peer is loopback, so the address is taken from
    Cloudflare's ``CF-Connecting-IP`` header when it holds a valid IP address.
    From any other peer the header is ignored. Run nothing but cloudflared in
    front of the connector: another local proxy that passes the header through
    unchanged would let a client choose the address the throttle counts
    against.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return "unknown"
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        header = request.headers.get("cf-connecting-ip", "").strip()
        try:
            return str(ipaddress.ip_address(header))
        except ValueError:
            pass
    return peer


def token_id(token: str) -> str:
    """A short, stable id for a bearer token that does not reveal it."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class MailReads:
    """When each MCP session and each token last read mail."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._at: dict[str, datetime] = {}

    def mark(self, keys: Sequence[str]) -> None:
        with self._lock:
            now = self._clock()
            self._at = {k: t for k, t in self._at.items() if now - t < MAIL_HOLD}
            for key in keys:
                self._at[key] = now

    def recent(self, keys: Sequence[str]) -> bool:
        """True when any of ``keys`` read mail in the last 30 minutes."""
        with self._lock:
            now = self._clock()
            return any(
                (read_at := self._at.get(key)) is not None and now - read_at < MAIL_HOLD
                for key in keys
            )


def _alert_callback(config: ConnectorConfig) -> Callable[[str], None]:
    """Send a sign-in alert as a reminder through the notify helper, or log it when there is none."""

    def alert(message: str) -> None:
        try:
            notify_module = importlib.import_module("src.brain.connector.notify")
        except ImportError:
            _logger.warning("sign-in alert not sent, the notify helper is missing: %s", message)
            return
        result = notify_module.notify(
            ALERT_TITLE,
            config.alert_lists.household,
            notes=message,
            enabled=config.integrations.reminders,
            timezone=config.timezone,
        )
        if inspect.isawaitable(result):
            asyncio.run(_wait(result))

    return alert


async def _wait(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


class _Arguments(ArgModelBase):
    """Hands the tool body the arguments as sent; ``run_tool`` checks them against the schema."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        return {"arguments": dict(self.model_extra or {})}


def _library_session_id(request_context: Any) -> str | None:
    """The session id the library assigned and checks, or None (the 2026-07-28 protocol has none).

    The ``Mcp-Session-Id`` header itself is never trusted: on the 2026-07-28
    path the library ignores it, so any value would pass. ``MCPServer``'s
    ``Context`` does not expose the connection, so the id is read from the
    session's connection; if a library change hides it, this returns None and
    only the token key is used.
    """
    connection = getattr(getattr(request_context, "session", None), "_connection", None)
    session_id = getattr(connection, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _caller(context: Context[Any, Any]) -> tuple[str, list[str]]:
    """The token id and the mail-state keys of the request a tool runs in.

    The keys are the token and, when there is one, the library's session.
    """
    try:
        request_context = context.request_context
    except ValueError:
        return "none", ["token:none"]
    request = getattr(request_context, "request", None)
    user = request.scope.get("user") if isinstance(request, Request) else None
    token = user.access_token.token if isinstance(user, AuthenticatedUser) else ""
    tid = token_id(token) if token else "none"
    keys = [f"token:{tid}"]
    session_id = _library_session_id(request_context)
    if session_id is not None:
        keys.append(f"session:{session_id}")
    return tid, keys


async def _audit(
    audit: AuditLog, tool: ToolDef, arguments: dict[str, Any], text: str, tid: str
) -> None:
    if text.startswith("ERROR:"):
        result = f"error: {text.removeprefix('ERROR:').strip()}"
    elif text.startswith(HELD_PREFIX):
        rule = next((line for line in text.splitlines() if line.startswith(HELD_RULE)), "")
        result = f"held: {rule.removeprefix(HELD_RULE)}"
    else:
        result = "ok"
    try:
        summary = json.dumps(audit_args(arguments), ensure_ascii=False, sort_keys=True, default=str)
        await anyio.to_thread.run_sync(
            audit.write, tool.name, audit_target(arguments), result, tid, summary
        )
    except Exception:
        _logger.exception("audit line for %s not written", tool.name)


def _mcp_tool(tool: ToolDef, env: ToolEnv, audit: AuditLog, mail: MailReads) -> Tool:
    async def handler(arguments: dict[str, Any], context: Context[Any, Any]) -> CallToolResult:
        # Shielded: a cancelled call (the owner pressing Stop, or a closed
        # response stream) still finishes its write under the write lock and
        # writes its audit line. The cancel takes effect once the call returns.
        with anyio.CancelScope(shield=True):
            tid, keys = _caller(context)
            if tool.name in MAIL_READ_TOOLS:
                mail.mark(keys)
            text = "ERROR: the call stopped before it finished"
            try:
                text = await run_tool(
                    tool, env, arguments, Call(mail_read_recently=mail.recent(keys))
                )
            finally:
                await _audit(audit, tool, arguments, text, tid)
        failed = text.startswith("ERROR:")
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=failed)

    annotations = (
        ToolAnnotations(read_only_hint=True)
        if tool.read_only
        else ToolAnnotations(read_only_hint=False, destructive_hint=tool.destructive)
    )
    return Tool(
        fn=handler,
        name=tool.name,
        title=None,
        description=tool.description,
        parameters=deepcopy(tool.schema),
        fn_metadata=FuncMetadata(arg_model=_Arguments),
        is_async=True,
        context_kwarg="context",
        annotations=annotations,
    )


def _transport_security(config: ConnectorConfig) -> TransportSecuritySettings:
    public = urlparse(config.public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        allowed_origins=[f"{public.scheme}://{public.netloc}", *CLAUDE_ORIGINS],
    )


def _housekeeping(audit: AuditLog, undo: UndoStore) -> None:
    now = datetime.now(UTC)
    try:
        audit.prune(now)
    except OSError as error:
        _logger.warning("old audit files not removed: %s", error)
    try:
        undo.purge(now)
    except (OSError, sqlite3.Error) as error:
        _logger.warning("old undo rows not removed: %s", error)


async def _health(request: Request) -> Response:
    return PlainTextResponse("ok")


def build_app(config: ConnectorConfig, *, icloud: ICloudCall = icloud_bridge) -> Starlette:
    """The connector's whole web app; ``icloud`` runs the iCloud read tools."""
    config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = AuthStore(config.data_dir / AUTH_DB)
    undo = UndoStore(config.data_dir / UNDO_DB)
    audit = AuditLog(config.data_dir / AUDIT_DIR)
    _housekeeping(audit, undo)
    provider = VaultOAuthProvider(config, store)
    throttle = Throttle(alert=_alert_callback(config))
    env = ToolEnv(
        vault=config.vault_path,
        undo=undo,
        icloud=icloud,
        zone=ZoneInfo(config.timezone),
        layout=config.layout,
    )
    mail = MailReads()
    if not config.instructions_overlay.is_file():
        _logger.warning(
            "%s is missing; the connector serves its generic rules", config.instructions_overlay
        )
    instructions = render_instructions(
        load_overlay(config.instructions_overlay, config.profile_path),
        config.layout,
        config.integrations,
    )
    atomic_write_text(
        config.data_dir / INSTRUCTIONS_FILE,
        instructions,
        mode=0o600,
        preserve_existing_mode=False,
    )
    base = config.public_url.rstrip("/")
    security = _transport_security(config)

    def endpoint(name: str, path: str, chosen: list[ToolDef]) -> tuple[MCPServer[Any], Starlette]:
        auth = AuthSettings.model_validate(
            {
                "issuer_url": config.public_url,
                "resource_server_url": f"{base}{path}",
                "validate_token_resource": True,
                "client_registration_options": {"enabled": True},
                "revocation_options": {"enabled": True},
            }
        )
        server: MCPServer[Any] = MCPServer(
            name=name,
            instructions=instructions,
            auth_server_provider=provider,
            auth=auth,
            tools=[_mcp_tool(tool, env, audit, mail) for tool in chosen],
        )
        return server, server.streamable_http_app(
            streamable_http_path=path, transport_security=security
        )

    served = build_tools(config)
    full, full_app = endpoint("Vault", "/mcp", [tool for tool in served if tool.full])
    limited, limited_app = endpoint(
        "Vault, limited", "/mcp/limited", [tool for tool in served if tool.limited]
    )

    routes: list[BaseRoute] = [
        Route("/health", _health, methods=["GET"]),
        *login_routes(provider, store, throttle, address=connector_address),
    ]
    taken = {"/health", "/login"}
    for app in (full_app, limited_app):
        for route in app.routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and path not in taken:
                routes.append(Route(path, endpoint=app))
                taken.add(path)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with full.session_manager.run(), limited.session_manager.run():
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def use_timezone(name: str) -> None:
    """Make the reminder and calendar handlers, and the iCloud tool processes, use ``name``.

    They read ``TIMEZONE`` from the orchestrator settings, which take it from
    the environment or ``.env``; the connector's own setting wins over both.
    """
    os.environ["TIMEZONE"] = name
    orchestrator_config.TIMEZONE = name


def serve(config: ConnectorConfig) -> None:
    """Run the connector on 127.0.0.1 until stopped, in the configured time zone."""
    import uvicorn

    use_timezone(config.timezone)

    uvicorn.run(
        build_app(config),
        host="127.0.0.1",
        port=config.port,
        proxy_headers=False,
        server_header=False,
    )
