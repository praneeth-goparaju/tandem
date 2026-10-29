"""One tiny live run through the Claude runner, to check sign-in and isolation.

It makes one short Claude Haiku call on the subscription that Claude Code is
signed in with, so it refuses to run without --confirm. It prints the tools and
MCP servers the session offered, the run status and the token counts. The run
writes its telemetry row to a temporary database, never the live one.

    .venv/bin/python tools/claude_smoke.py --confirm [--print-init]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENT = {
    "name": "smoke",
    "model": "claude-haiku-5-5",
    "effort": "low",
    "tools": ["Read"],
    "max_turns": 2,
}


async def _run(print_init: bool) -> int:
    import claude_agent_sdk
    from claude_agent_sdk import SystemMessage

    from src.orchestrator.runners.claude_runner import ClaudeRunner

    captured: dict[str, Any] = {}

    def recording_query(*, prompt: Any, options: Any):
        async def stream():
            async for message in claude_agent_sdk.query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    captured["init"] = message.data
                yield message

        return stream()

    async def no_notice(_text: str) -> None:
        return None

    runner = ClaudeRunner(query_fn=recording_query, notify_fn=no_notice)
    result = await runner.run(dict(AGENT), "Reply with the single word ready.", {
        "system_prompt": "You are a connectivity check. Do exactly what the user asks.",
        "agent_name": "smoke",
        "timeout": 180,
    })
    init = captured.get("init") or {}
    servers = [s.get("name") if isinstance(s, dict) else s for s in init.get("mcp_servers") or []]
    print(f"init tools:       {init.get('tools')}")
    print(f"init mcp_servers: {servers}")
    print(f"status:           {result['status']}")
    print(f"error:            {result.get('error')}")
    print(f"model:            {result['model']}")
    print(f"response:         {result['response']!r}")
    print(f"tokens:           in={result['input_tokens']} out={result['output_tokens']} "
          f"cache_read={result['cache_read_tokens']}")
    if print_init:
        print(json.dumps(init, indent=2, default=str))
    return 0 if result["status"] == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="make the live Claude call (uses the subscription)")
    parser.add_argument("--print-init", action="store_true",
                        help="print the raw session init data")
    args = parser.parse_args()
    if not args.confirm:
        print("Refusing to start: this makes a live Claude call. Re-run with --confirm.")
        return 2
    # Resolved, because the database layer refuses a path through a symlink
    # and macOS temp folders sit behind /var -> /private/var.
    scratch = Path(tempfile.mkdtemp(prefix="claude-smoke-")).resolve()
    os.environ["MEMORY_DB_PATH"] = str(scratch / "smoke.db")
    os.environ["VAULT_DB_PATH"] = str(scratch / "smoke.db")
    sys.path.insert(0, str(REPO_ROOT))
    return asyncio.run(_run(args.print_init))


if __name__ == "__main__":
    raise SystemExit(main())
