"""Gateway-owned schemas for sensitive public tools."""

from __future__ import annotations

from typing import Any

SEND_BOT_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "area": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
            "description": (
                "Configured Brain area name. The private backend resolves the Slack route."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4_000,
            "description": "Plain-text message body.",
        },
        "title": {
            "type": "string",
            "maxLength": 120,
            "description": "Optional short plain-text title.",
        },
    },
    "required": ["area", "message"],
    "additionalProperties": False,
}

PINNED_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "send_bot_message": SEND_BOT_MESSAGE_SCHEMA,
}
