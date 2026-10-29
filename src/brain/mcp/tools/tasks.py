from __future__ import annotations

import re

from src.brain.actions import task_links
from src.brain.mcp._common import _bracket, _err, _ok, _wrap_external, tasks
from src.brain.mcp.tool_registry import brain_tool

# Todoist v1 ids are alphanumeric (e.g. "6gPh3fGCxXPhFgHX"); EventKit
# calendarItemIdentifier values are hyphenated UUIDs. Accept both —
# length/charset only, no format lock-in.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

_ACTION_NOTE_ID_RE = re.compile(r"^A-\d{8}-\d{2}$", re.IGNORECASE)
_COMPLETED_NOTES_LIMIT = 160
_MAX_COMPLETED_ACTION_IDS = 5


def _valid_task_id(tid: str) -> bool:
    return bool(tid) and bool(_TASK_ID_RE.match(tid))


def _title_of(t: dict) -> str:
    return str(t.get("content") or t.get("title") or "")


def _due_of(t: dict) -> str:
    due = t.get("due")
    if isinstance(due, dict):
        return str(due.get("date") or "")
    return str(due or "")


def _completed_note_context(description: object) -> str:
    return " ".join(str(description or "").split())[:_COMPLETED_NOTES_LIMIT]


@brain_tool(
    name="tasks_list",
    description=(
        "List entries from the user's task list (active backend: Todoist "
        "projects or Apple Reminders lists). Default: pending tasks across "
        "all projects/lists. Pass completed=true for recently finished tasks. "
        "Call this BEFORE tasks_create to avoid duplicates and to see the "
        "project/list names that task creation needs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Filter to one project (Todoist project id) or Reminders list name. Optional.",
            },
            "completed": {
                "type": "boolean",
                "description": "List recently completed tasks instead of pending ones. Default false.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results for completed listing (default: 30).",
            },
        },
    },
)
async def tasks_list(args):
    try:
        if args.get("completed"):
            items = await tasks().list_completed(limit=args.get("limit", 30))
        else:
            items = await tasks().list_pending(project=args.get("project"))
    except RuntimeError as e:
        return _ok(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"task backend error: {e}")

    if not items:
        return _ok("No completed tasks found." if args.get("completed") else "No active tasks.")

    lines = []
    if args.get("completed"):
        for t in items:
            done = str(t.get("completed_at", ""))[:10]
            raw_task_id = str(t.get("id") or t.get("task_id") or "")
            task_id = raw_task_id if _valid_task_id(raw_task_id) else ""
            if task_id:
                task_links.mark_completed(task_id, str(t.get("completed_at") or "") or None)
            title = " ".join(_title_of(t).split())[:80]
            line = f"[done {done}] [id:{task_id or '?'}] {_bracket(title)}"
            all_action_ids = task_links.action_ids_for_task(task_id) if task_id else []
            action_ids = all_action_ids[:_MAX_COMPLETED_ACTION_IDS]
            note_context = _completed_note_context(t.get("description"))
            if action_ids:
                line += f"  action_ids:{','.join(action_ids)}"
                if len(action_ids) > 1 or len(all_action_ids) > len(action_ids):
                    line += "  action_ids_ambiguous:true"
            if note_context:
                line += (
                    "\nnotes:\n"
                    + _wrap_external(note_context, source="completed-task/notes")
                )
            lines.append(line)
    else:
        for t in items:
            raw_task_id = str(t.get("id") or "")
            task_id = raw_task_id if _valid_task_id(raw_task_id) else "?"
            metadata = (
                f"id={_bracket(task_id)} priority={_bracket(t.get('priority', 1))} "
                f"project={_bracket(t.get('project_id', '?'))}"
            )
            due = _due_of(t)
            if due:
                metadata += f" due={_bracket(due)}"
            title = _wrap_external(
                _title_of(t)[:80],
                source="pending-task/title",
            )
            lines.append(f"{metadata}\n{title}")
    return _ok("\n".join(lines))


@brain_tool(
    name="tasks_create",
    description=(
        "Create a task on the user's task list. Use for URGENT/IMPORTANT "
        "action items that have a concrete next step and deadline. Don't "
        "create tasks for FYI items. Write detailed descriptions — the user "
        "reads these on their phone. project_id is REQUIRED: call tasks_list "
        "first and pick the best-fit project/list (visible as project=[...]). "
        "Never default to Inbox — pick a specific project/list."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Task title — concise, actionable (e.g. 'Submit OpenAI Codex application')"},
            "description": {"type": "string", "description": "Detailed context: what to do, why it matters, links, prerequisites."},
            "due_date": {"type": "string", "description": "Deadline in YYYY-MM-DD format (e.g. '2026-04-17')"},
            "due_string": {"type": "string", "description": "Natural language date (e.g. 'tomorrow', 'next Monday'). Use due_date OR due_string, not both. Prefer due_date — not every backend parses natural language."},
            "priority": {"type": "integer", "description": "1=normal, 2=medium, 3=high, 4=urgent. Map: URGENT→4, IMPORTANT→3, FYI→1"},
            "project_id": {"type": "string", "description": "Destination project/list (REQUIRED — never use Inbox). A Todoist project id or an Apple Reminders list name; call tasks_list first to see what exists and pick the best match."},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels for categorization (e.g. ['career', 'deadline'])"},
            "action_note_id": {
                "type": "string",
                "pattern": r"^A-\d{8}-\d{2}$",
                "description": "Optional existing action-note ID this task implements.",
            },
            "rationale": {"type": "string", "description": "(Optional) why you're creating this task — recorded in the action queue for audit."},
            "source_finding_ids": {"type": "array", "items": {"type": "string"}, "description": "(Optional) finding ids that triggered this task."},
        },
        "required": ["content", "project_id"],
    },
)
async def tasks_create(args):
    from src.brain.mcp.tools.actions import propose_action

    action_note_id = str(args.get("action_note_id") or "").strip().upper()
    if action_note_id and not _ACTION_NOTE_ID_RE.fullmatch(action_note_id):
        return _err("action_note_id must match A-YYYYMMDD-NN")
    payload = {
        "content": args.get("content"),
        "description": args.get("description", ""),
        "project_id": args.get("project_id"),
        "priority": args.get("priority"),
        "due_date": args.get("due_date"),
        "due_string": args.get("due_string"),
        "labels": args.get("labels"),
        "action_note_id": action_note_id or None,
    }
    return await propose_action({
        "type": "create_todoist_task",
        "payload": payload,
        "rationale": args.get("rationale") or "agent-requested task creation",
        "source_finding_ids": args.get("source_finding_ids") or [],
        "risk": "auto",
    })
