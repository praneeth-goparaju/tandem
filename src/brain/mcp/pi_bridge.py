from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def _load_specs() -> dict[str, Any]:
    from src.brain.mcp.server import load_brain_tools

    return {spec.name: spec for spec in load_brain_tools()}


def _content(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return result["content"]
    if result is None:
        return [{"type": "text", "text": ""}]
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return [{"type": "text", "text": text}]


def _declares_error(result: Any, content: list[dict[str, Any]]) -> bool:
    if isinstance(result, dict) and (
        result.get("is_error") is True or result.get("isError") is True
    ):
        return True
    return any(
        block.get("type") == "text"
        and str(block.get("text") or "").lstrip().casefold().startswith("error:")
        for block in content
        if isinstance(block, dict)
    )


async def _call(payload: dict[str, Any]) -> dict[str, Any]:
    from src.brain.mcp.caller_context import caller_scope, parse_caller_context

    name = str(payload.get("name") or "")
    allowed = {str(item) for item in payload.get("allowed", [])}
    if name not in allowed:
        return {"is_error": True, "content": [{"type": "text", "text": "tool not allowed"}]}

    spec = _load_specs().get(name)
    if spec is None:
        return {"is_error": True, "content": [{"type": "text", "text": "unknown tool"}]}
    caller = parse_caller_context(payload.get("caller_context"))
    if caller is None:
        return {
            "is_error": True,
            "content": [{"type": "text", "text": "valid caller_context is required"}],
        }
    try:
        with caller_scope(caller):
            result = await spec.handler(payload.get("arguments") or {})
        content = _content(result)
        response: dict[str, Any] = {
            "is_error": _declares_error(result, content), "content": content,
        }
        if isinstance(result, dict) and result.get("outside_content"):
            response["outside_content"] = True
        return response
    except Exception as exc:  # noqa: BLE001
        return {
            "is_error": True,
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("list", "call"))
    args = parser.parse_args()
    specs = _load_specs()
    output: Any
    if args.operation == "list":
        output = [
            {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
            for spec in specs.values()
        ]
    else:
        output = asyncio.run(_call(json.load(sys.stdin)))
    json.dump(output, sys.stdout, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
