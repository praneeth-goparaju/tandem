from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from src.orchestrator.runners.run_guard import OutsideContentState, WebRiskBarrier

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_COMMAND = [sys.executable, "-m", "src.brain.mcp.pi_bridge"]
SERVER_NAME = "brain-tools"
DEFAULT_TOOL_TIMEOUT_S = 120.0
MAX_BRIDGE_OUTPUT_BYTES = 1_000_000

ACTION_RESULT_RE = re.compile(r"\baction\s+([a-f0-9]{12})\s+(?:queued|already pending)\b", re.I)
REPLY_DELIVERED_RE = re.compile(r"^reply:sent(?:\s|$)")


class ReplyDeliveryTracker:
    def __init__(self) -> None:
        self._delivered = False

    @property
    def delivered(self) -> bool:
        return self._delivered

    def observe(self, name: str, result: dict[str, Any]) -> None:
        if name != "reply_to_message" or result.get("is_error"):
            return
        for item in result.get("content") or []:
            if REPLY_DELIVERED_RE.search(str(item.get("text", "")).strip()):
                self._delivered = True


class ApprovalTracker:
    def __init__(self) -> None:
        self._ids: list[str] = []

    def observe(self, content: list[dict[str, Any]]) -> None:
        for item in content:
            for match in ACTION_RESULT_RE.finditer(str(item.get("text", ""))):
                action_id = match.group(1).lower()
                if action_id not in self._ids:
                    self._ids.append(action_id)

    def enrich_reply(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._ids or not isinstance(params, dict):
            return params
        explicit = [v for v in params.get("proposal_action_ids") or [] if isinstance(v, str)]
        return {**params, "proposal_action_ids": list(dict.fromkeys([*explicit, *self._ids]))}


MAX_MAIL_ARCHIVES_PER_RUN = 3
TOO_MANY_ARCHIVES = (
    f"At most {MAX_MAIL_ARCHIVES_PER_RUN} single mails can be proposed for archiving in one run. "
    "For more, use mail_archive_batch: one proposal with all of them (up to 50), one tap."
)


def _is_mail_archive(name: str, params: dict[str, Any]) -> bool:
    return name == "mail_archive" or (name == "propose_action" and params.get("type") == "mail_archive")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await proc.wait()


async def _run_bridge(operation: str, payload: Any, timeout_s: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *BRIDGE_COMMAND, operation,
        cwd=str(REPO_ROOT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def collect() -> tuple[bytes, bytes]:
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(b"" if payload is None else json.dumps(payload).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        out = bytearray()
        err = bytearray()

        async def pump(stream: asyncio.StreamReader, sink: bytearray) -> None:
            while chunk := await stream.read(65536):
                sink.extend(chunk)
                if len(out) + len(err) > MAX_BRIDGE_OUTPUT_BYTES:
                    raise RuntimeError("brain tool bridge output exceeded 1MB")

        await asyncio.gather(pump(proc.stdout, out), pump(proc.stderr, err))
        await proc.wait()
        return bytes(out), bytes(err)

    try:
        stdout, stderr = await asyncio.wait_for(collect(), timeout=timeout_s)
    except BaseException:
        await _terminate(proc)
        raise
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace")[-1000:]
        raise RuntimeError(f"brain tool bridge exited {proc.returncode}: {tail}")
    return stdout.decode("utf-8", "replace")


async def bridge_call(
    name: str,
    arguments: dict[str, Any],
    allowed: list[str],
    caller_context: dict[str, Any],
    *,
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
) -> dict[str, Any]:
    output = await _run_bridge("call", {
        "name": name,
        "arguments": arguments,
        "allowed": allowed,
        "caller_context": caller_context,
    }, timeout_s)
    result = json.loads(output)
    if not isinstance(result, dict):
        raise RuntimeError("brain tool bridge returned a non-object result")
    return result


async def load_brain_tool_specs() -> list[dict[str, Any]]:
    specs = json.loads(await _run_bridge("list", None, DEFAULT_TOOL_TIMEOUT_S))
    if not isinstance(specs, list):
        raise RuntimeError("brain tool bridge returned a non-list tool catalogue")
    return specs


def require_known_brain_tools(requested: list[str], specs: list[dict[str, Any]]) -> list[str]:
    known = {spec["name"] for spec in specs}
    unknown = list(dict.fromkeys(name for name in requested if name not in known))
    if unknown:
        raise ValueError(f"Unknown brain tool(s): {', '.join(unknown)}")
    return requested


def _object_schema(schema: Any) -> dict[str, Any]:
    """A JSON schema the SDK passes through unchanged: it needs `type` and `properties`."""
    result = dict(schema) if isinstance(schema, dict) else {}
    result.setdefault("type", "object")
    result.setdefault("properties", {})
    return result


def make_brain_tools(
    specs: list[dict[str, Any]],
    allowed: list[str],
    caller_context: dict[str, Any],
    *,
    barrier: WebRiskBarrier,
    approvals: ApprovalTracker,
    replies: ReplyDeliveryTracker,
    verify: bool = False,
    state: OutsideContentState | None = None,
) -> list[SdkMcpTool[Any]]:
    allowed_set = set(allowed)
    mail_archives = 0

    def make(spec: dict[str, Any]) -> SdkMcpTool[Any]:
        name = spec["name"]

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal mail_archives
            params = dict(args or {})
            if _is_mail_archive(name, params):
                if mail_archives >= MAX_MAIL_ARCHIVES_PER_RUN:
                    return {"content": [{"type": "text", "text": TOO_MANY_ARCHIVES}], "is_error": True}
                mail_archives += 1
            if name == "reply_to_message":
                params = approvals.enrich_reply(params)
            if name == "propose_action" and (barrier.web_seen or verify):
                params = {**params, "risk": "confirm"}
            try:
                result = await bridge_call(name, params, allowed, caller_context)
            except Exception as exc:  # noqa: BLE001
                return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
            replies.observe(name, result)
            if state is not None:
                state.note_result(result)
            content = list(result.get("content") or [])
            if not result.get("is_error"):
                approvals.observe(content)
            return {"content": content, "is_error": bool(result.get("is_error"))}

        return SdkMcpTool(
            name=name,
            description=str(spec.get("description") or ""),
            input_schema=_object_schema(spec.get("input_schema")),
            handler=handler,
        )

    return [make(spec) for spec in specs if spec["name"] in allowed_set]


def build_brain_server(
    specs: list[dict[str, Any]],
    allowed: list[str],
    caller_context: dict[str, Any],
    *,
    barrier: WebRiskBarrier,
    approvals: ApprovalTracker,
    replies: ReplyDeliveryTracker,
    verify: bool = False,
    state: OutsideContentState | None = None,
) -> McpSdkServerConfig:
    tools = make_brain_tools(
        specs, allowed, caller_context,
        barrier=barrier, approvals=approvals, replies=replies, verify=verify, state=state,
    )
    return create_sdk_mcp_server(SERVER_NAME, tools=tools)
