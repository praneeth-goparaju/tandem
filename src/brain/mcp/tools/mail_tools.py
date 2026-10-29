from __future__ import annotations

from src.brain.actions.types import ActionType
from src.brain.mcp._common import _bracket, _err, _ok, _wrap_external
from src.brain.mcp.tool_registry import brain_tool
from src.brain.mcp.tools import mail_ops
from src.brain.mcp.tools._mail_providers import MailNotConfigured
from src.brain.mcp.tools.icloud_actions import _payload, _propose

_PROVIDER_PROP = {"type": "string", "enum": ["icloud", "gmail"], "description": "Mail account."}
@brain_tool(
    name="mail_list_attachments",
    description="List a message's attachments (index, filename, content_type, size). Args: provider, uid, optional mailbox.",
    input_schema={
        "type": "object",
        "properties": {"provider": _PROVIDER_PROP, "uid": {"type": "string"}, "mailbox": {"type": "string"}},
        "required": ["provider", "uid"],
    },
)
async def mail_list_attachments(args):
    try:
        atts = await mail_ops.list_attachments(args["provider"], args["uid"], (args.get("mailbox") or "INBOX"))
        if not atts:
            return _ok("(no attachments)")
        lines = [
            (
                f"[{a['index']}] {_bracket(a['filename'])} "
                f"({_bracket(a['content_type'])}, {a['size']} bytes)"
            )
            for a in atts
        ]
        return _ok("\n".join(lines))
    except (MailNotConfigured, ValueError, RuntimeError) as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"mail_list_attachments failed: {type(e).__name__}: {e}")


@brain_tool(
    name="mail_get_attachment",
    description=(
        "Read one small text attachment as explicitly untrusted inline content. "
        "Binary files are rejected and are never written to disk. Args: provider, uid, "
        f"index, optional mailbox. Max {mail_ops.MAX_ATTACHMENT_BYTES} bytes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": _PROVIDER_PROP,
            "uid": {"type": "string"},
            "index": {"type": "integer"},
            "mailbox": {"type": "string"},
        },
        "required": ["provider", "uid", "index"],
    },
)
async def mail_get_attachment(args):
    try:
        out = await mail_ops.get_attachment(
            args["provider"],
            args["uid"],
            int(args["index"]),
            (args.get("mailbox") or "INBOX"),
        )
        metadata = (
            f"attachment {_bracket(out['filename'])} "
            f"({_bracket(out['content_type'])}, {out['size']} bytes)"
        )
        content = _wrap_external(
            out["content"],
            source=f"mail attachment/{out['filename']}",
        )
        return _ok(f"{metadata}\n{content}")
    except (MailNotConfigured, ValueError, RuntimeError) as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"mail_get_attachment failed: {type(e).__name__}: {e}")


@brain_tool(
    name="mail_archive_batch",
    description=(
        "Archive several messages of one account and mailbox with one approval tap: "
        "the owner sees the count and the senders, and one tap archives them all "
        "(moved out of the inbox, never deleted). Use it whenever more than one mail "
        "should go. Required: provider, uids (1 to 50), summary (a few words, e.g. "
        "'34 GitHub CI failure notices'), rationale. Optional: mailbox."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": _PROVIDER_PROP,
            "uids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
            "mailbox": {"type": "string"},
            "summary": {"type": "string", "maxLength": 200},
            "rationale": {"type": "string"},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["provider", "uids", "summary", "rationale"],
    },
)
async def mail_archive_batch(args):
    payload = _payload(args, ("provider", "uids", "mailbox", "summary"))
    return await _propose(ActionType.MAIL_ARCHIVE_BATCH, payload, args)


@brain_tool(
    name="mail_archive",
    description=(
        "Archive one message (remove it from the inbox without deleting it). "
        "A trusted local interactive MCP client executes it immediately through "
        "the audited action queue. Scheduled, unknown, or tainted callers still "
        "require Slack confirmation. Required: provider, uid, rationale. Optional: "
        "mailbox. Omit risk; the server-owned caller policy decides the gate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": _PROVIDER_PROP,
            "uid": {"type": "string"},
            "mailbox": {"type": "string"},
            "rationale": {"type": "string"},
            "risk": {"type": "string", "enum": ["auto", "confirm", "manual"]},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["provider", "uid", "rationale"],
    },
)
async def mail_archive(args):
    payload = _payload(args, ("provider", "uid", "mailbox"))
    return await _propose(ActionType.MAIL_ARCHIVE, payload, args)
