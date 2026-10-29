from __future__ import annotations

import json
import re
from datetime import date

from src.brain.atomic_io import atomic_create_text
from src.brain.mcp.tool_registry import brain_tool

_KINDS = {"reply", "email", "message", "doc"}


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "draft"


@brain_tool(
    name="create_draft",
    description=(
        "Write a draft artifact (email reply, message, short doc) to the vault "
        "for the user to review and send. Use this instead of just creating a "
        "Todoist task when the work is mostly writing — give the user something "
        "to approve, not another reminder. The draft is saved under "
        "'00 - Inbox/drafts/YYYY-MM-DD-<slug>.md' with frontmatter "
        "(kind, recipient, status: pending, source_finding_id). Returns the "
        "relative vault path so you can include it in the notification."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(_KINDS), "description": "Type of draft: reply, email, message, doc."},
            "title": {"type": "string", "description": "Short title — used for filename slug and shown in the file header."},
            "recipient": {"type": "string", "description": "Who the draft is addressed to (person, company, channel). Empty string for docs."},
            "body": {"type": "string", "description": "The full draft body, ready to send. Markdown allowed. Be concrete — this is what the user will copy/send."},
            "context": {"type": "string", "description": "Optional 1-2 sentence note on why this draft was written (for the user reviewing it)."},
            "finding_id": {"type": "string", "description": "Source finding id, if this draft was prompted by a finding."},
        },
        "required": ["kind", "title", "body"],
    },
)
async def create_draft(args):
    from src.orchestrator.config import VAULT_PATH

    drafts_dir = VAULT_PATH / "00 - Inbox" / "drafts"
    kind = args["kind"]
    if kind not in _KINDS:
        return {"content": [{"type": "text", "text": f"ERROR: kind must be one of {sorted(_KINDS)}"}]}

    title = args["title"].strip()
    recipient = args.get("recipient", "").strip()
    body = args["body"]
    context = args.get("context", "").strip()
    finding_id = args.get("finding_id", "").strip()

    today = date.today().isoformat()
    slug = _slugify(title)
    path = drafts_dir / f"{today}-{kind}-{slug}.md"

    fm = [
        "---", f"kind: {kind}", f"title: {json.dumps(title, ensure_ascii=False)}",
        "status: pending", f"created: {today}",
    ]
    if recipient:
        fm.append(f"recipient: {json.dumps(recipient, ensure_ascii=False)}")
    if finding_id:
        fm.append(f"source_finding_id: {json.dumps(finding_id, ensure_ascii=False)}")
    fm.append("---")

    parts = ["\n".join(fm), "", f"# {title}", ""]
    if recipient:
        parts.append(f"**To:** {recipient}")
        parts.append("")
    if context:
        parts.append(f"> {context}")
        parts.append("")
    parts.append(body.rstrip() + "\n")

    text = "\n".join(parts)
    n = 1
    while True:
        try:
            atomic_create_text(path, text)
            break
        except FileExistsError:
            n += 1
            path = drafts_dir / f"{today}-{kind}-{slug}-{n}.md"

    rel = path.relative_to(VAULT_PATH)
    return {"content": [{"type": "text", "text": f"Draft created: {rel}"}]}
