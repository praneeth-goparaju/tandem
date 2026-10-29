from __future__ import annotations

from src.brain.mcp.tool_registry import brain_tool
from src.brain.mcp.tools.media import media_ids_schema


@brain_tool(
    name="reply_to_message",
    description=(
        "Send a reply message on the caller's authenticated area channel, optionally threaded "
        "under an original message. Use this to talk back to the user on the "
        "same channel their message arrived on. For proactive alerts that "
        "start a new thread, use push_notify instead.\n\n"
        "Formatting: write normal markdown (`**bold**`, `*italic*`, `~strike~`, "
        "inline `code`). The Slack notifier translates to Slack mrkdwn on the "
        "way out. No tables. Long content goes in the vault (draft or "
        "analysis file) and the reply points at it. To show approval controls "
        "for same-area actions you just proposed, pass their exact ids through "
        "`proposal_action_ids`; ids written only in reply prose are ignored."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Reply body. Markdown: **bold**, *italic*, ~strike~, `code`. No tables."},
            "quote_timestamp": {"type": "string", "description": "Original Slack message ts (e.g. '1712345678.000123') to thread under. Optional — omit for unthreaded replies."},
            "finding_id": {"type": "string", "description": "Optional finding id — recorded with the message (DB audit row + Slack metadata) so follow-up replies in-thread route back to this finding."},
            "proposal_action_ids": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[a-fA-F0-9]{12}$"},
                "maxItems": 10,
                "uniqueItems": True,
                "description": "Exact propose_action ids to render as same-area approval cards.",
            },
            "media_ids": media_ids_schema("under this reply"),
        },
        "required": ["text"],
    },
)
async def reply_to_message(args):
    from src.brain.mcp._common import _err, notifier
    from src.brain.mcp.caller_context import require_current_caller
    from src.brain.media.store import parse_media_ids
    from src.brain.notifications_audit import record_notification
    from src.brain.plugins.base import media_outcome_note

    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return {"content": [{"type": "text", "text": f"ERROR: {exc}"}]}
    if not caller.is_reply:
        return {
            "content": [{
                "type": "text",
                "text": "ERROR: reply_to_message requires an authenticated inbound reply",
            }]
        }
    try:
        media_ids = parse_media_ids(args.get("media_ids"))
    except ValueError as exc:
        return _err(str(exc))
    text = args.get("text", "")
    area = caller.area or caller.agent
    agent = caller.agent
    quote_ts = args.get("quote_timestamp")
    finding_id = args.get("finding_id")

    n = notifier()
    try:
        result = await n.send(
            text,
            title="",
            urgency="info",
            finding_id=finding_id,
            area=area,
            agent=agent,
            quote_timestamp=str(quote_ts) if quote_ts else None,
            proposal_action_ids=list(args.get("proposal_action_ids") or []),
            **({"media_ids": media_ids} if media_ids else {}),
        )
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"reply failed: {e}"}]}

    status = result.get("status", "unknown")
    if status in {"sent", "noop"}:
        record_notification(
            agent=agent,
            area=area,
            title="(reply)",
            message=text,
            status=status,
            signal_ts=result.get("id"),
            finding_id=finding_id,
            group_id=result.get("group_id"),
        )

    answer = f"reply:{status} id={result.get('id')}"
    note = media_outcome_note(result)
    return {"content": [{"type": "text", "text": f"{answer}\n{note}" if note else answer}]}
