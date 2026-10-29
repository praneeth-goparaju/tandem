from __future__ import annotations

from src.brain.actions.bot_message_policy import (
    BRAIN_AREA_PATTERN,
    MAX_BOT_MESSAGE_CHARS,
    MAX_BOT_TITLE_CHARS,
    MAX_BRAIN_AREA_CHARS,
    normalize_bot_message_payload,
)
from src.brain.mcp._common import _err
from src.brain.mcp.tool_registry import brain_tool
from src.brain.mcp.tools.media import media_ids_schema


@brain_tool(
    name="send_bot_message",
    description=(
        "Send one plain-text Slack message as the configured Brain bot to a "
        "configured Brain area route. This tool is available only to the trusted "
        "interactive MCP profile. The server resolves the Slack channel and bot "
        "token internally; channel IDs, usernames, webhooks, tokens, attachments, "
        "approval controls, and arbitrary Slack formatting are not accepted. "
        "Use an area name from the Brain registry. The send is recorded in the "
        "action ledger and executes without a second Slack approval round-trip."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BRAIN_AREA_CHARS,
                "pattern": BRAIN_AREA_PATTERN,
                "description": (
                    "Configured Brain area name. The server resolves its Slack route; "
                    "raw Slack destinations are not accepted."
                ),
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BOT_MESSAGE_CHARS,
                "description": "Plain-text message body.",
            },
            "title": {
                "type": "string",
                "maxLength": MAX_BOT_TITLE_CHARS,
                "description": "Optional short plain-text title.",
            },
        },
        "required": ["area", "message"],
        "additionalProperties": False,
    },
)
async def send_bot_message(args):
    from src.brain.actions.types import ActionType
    from src.brain.mcp.caller_context import require_current_caller
    from src.brain.mcp.tools.actions import propose_action

    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    action_type = ActionType.SEND_BOT_MESSAGE.value
    if (
        caller.agent != "external-mcp"
        or caller.area != "interactive-mcp"
        or caller.is_reply
        or caller.untrusted_input
        or action_type not in caller.allowed_action_types
        or action_type not in caller.direct_action_types
    ):
        return _err("send_bot_message requires the trusted direct MCP capability")

    try:
        destination_area, message, title = normalize_bot_message_payload(
            {
                "destination_area": args.get("area"),
                "message": args.get("message"),
                "title": args.get("title", ""),
            }
        )
    except ValueError as exc:
        return _err(str(exc))

    return await propose_action(
        {
            "type": action_type,
            "payload": {
                "destination_area": destination_area,
                "message": message,
                "title": title,
            },
            "rationale": "trusted interactive client requested a Brain bot message",
        }
    )


@brain_tool(
    name="push_notify",
    description=(
        "Send a push notification to the user via the configured Notifier plugin "
        "(Slack or noop). Be specific and self-contained — include the "
        "key facts, why it matters, and the concrete next step. The user reads "
        "this on their phone without opening the vault.\n\n"
        "Formatting: write regular markdown (`**bold**`, `*italic*`, `~strike~`, "
        "inline `code`). The Slack notifier translates it on the way out: a "
        "`---` line becomes a divider, a `## Heading` line a bold underlined "
        "heading, and consecutive `- ` lines a real bullet list. "
        "Do NOT use markdown tables — they render as raw text. Use "
        "short lines and bullets with - or •. Long content goes in the vault; "
        "a push points at it.\n\n"
        "To attach approval controls for actions you just proposed, pass their "
        "exact ids in `proposal_action_ids`. Merely writing an action id in the "
        "title, message, or thread detail never creates approval controls.\n\n"
        "One topic per message — if you have three things to say, send three "
        "pushes. Bundled messages are confusing to react to and collapse into "
        "a wall of text on the phone. Use `thread_detail` for the evidence, "
        "analysis, and next steps for that one topic: Slack will post the short "
        "message as a channel root and the detail as its first threaded reply.\n\n"
        "Bad: 'Pollen is high today'. "
        "Good: 'Birch pollen HIGH, ash MEDIUM in <your city>. Take your allergy tablet before going outside. Avoid 10-16h window.'\n\n"
        "Threading a follow-up: the response includes `ts=<slack_timestamp>`. "
        "Pass that to `reply_to_message(quote_timestamp=<ts>, text=...)` "
        "to attach a detailed analysis under the parent message — the parent stays a "
        "scannable 2-liner in the channel, the depth lives in the thread. Useful "
        "for housing listings, job postings, anything with 'short summary + long "
        "analysis' shape."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title (under 50 chars)"},
            "message": {"type": "string", "description": "Short channel-level summary for this one topic. Put deeper evidence and analysis in thread_detail."},
            "thread_detail": {"type": "string", "description": "Optional deeper detail for this same topic. It is posted as the first reply under the root message, creating a dedicated Slack thread."},
            "finding_id": {"type": "string", "description": "Finding ID to link this notification to (from store_finding or get_recent_findings result)."},
            "proposal_action_ids": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[a-fA-F0-9]{12}$"},
                "maxItems": 10,
                "uniqueItems": True,
                "description": (
                    "Exact ids returned by propose_action for same-area approval cards. "
                    "Ids mentioned only in prose are intentionally ignored."
                ),
            },
            "occasion_person_id": {
                "type": "string",
                "description": (
                    "For a humans-agent occasion reminder only: exact person_id from "
                    "occasions_scan. Supply with occasion and occasion_year."
                ),
            },
            "occasion": {
                "type": "string",
                "description": (
                    "For a humans-agent occasion reminder only: exact occasion id from "
                    "occasions_scan. Supply with occasion_person_id and occasion_year."
                ),
            },
            "occasion_year": {
                "type": "integer",
                "description": (
                    "For a humans-agent occasion reminder only: exact year from "
                    "occasions_scan. The complete marker is validated and shown for review."
                ),
            },
            "media_ids": media_ids_schema("under this notification"),
            "rationale": {"type": "string", "description": "(Optional) why this notification — recorded in the action queue for audit."},
        },
        "required": ["title", "message"],
    },
)
async def push_notify(args):
    from src.brain.mcp.caller_context import require_current_caller
    from src.brain.mcp.tools.actions import propose_action
    from src.brain.media.store import parse_media_ids

    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return {"content": [{"type": "text", "text": f"ERROR: {exc}"}]}
    try:
        media_ids = parse_media_ids(args.get("media_ids"))
    except ValueError as exc:
        return _err(str(exc))
    agent = caller.agent
    area = caller.area or caller.agent
    payload = {
        "title": args.get("title", ""),
        "message": args.get("message", ""),
        "thread_detail": args.get("thread_detail", ""),
        "agent": agent,
        "area": area,
        "finding_id": args.get("finding_id"),
        "proposal_action_ids": list(args.get("proposal_action_ids") or []),
    }
    if media_ids:
        payload["media_ids"] = media_ids
    for field in ("occasion_person_id", "occasion", "occasion_year"):
        if field in args:
            payload[field] = args[field]
    return await propose_action(
        {
            "type": "push_notification",
            "payload": payload,
            "rationale": args.get("rationale") or "agent-requested notification",
            "source_finding_ids": (
                [args["finding_id"]] if args.get("finding_id") else []
            ),
            "risk": "auto",
        },
        _fixed_route_notification=True,
    )
