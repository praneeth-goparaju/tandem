from __future__ import annotations

import json

from src.brain.atomic_io import confined_path, read_regular_text
from src.brain.mcp._common import _bracket, _err, _ok, _wrap_external
from src.brain.mcp.caller_context import require_current_caller
from src.brain.mcp.tool_registry import brain_tool


def _require_authenticated_reply() -> None:
    caller = require_current_caller()
    if not caller.is_reply:
        raise PermissionError("human-diary writes are available only during an authenticated reply")


@brain_tool(
    name="humans_find",
    description=(
        "Find people in the diary by name (substring, case-insensitive) to resolve "
        "who a note is about. Returns id, name, tier, number, last_interaction. "
        "Call this before logging; if there are multiple matches, ask the user "
        "which one. If there are none, the person is new — use humans_add_person."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
async def humans_find(args):
    from src.brain.occasions import store

    q = (args.get("query") or "").strip().lower()
    if not q:
        return _err("query is required")
    hits = [
        {"id": p.id, "name": p.name, "tier": p.tier, "e164": p.e164, "last_interaction": p.last_interaction}
        for p in store.load_people().people
        if q in (p.name or "").lower()
    ]
    return _ok(_wrap_external(
        json.dumps({"matches": hits}, indent=2, ensure_ascii=False),
        source="humans/lookup",
    ))


@brain_tool(
    name="humans_add_person",
    description=(
        "Add a NEW person to the diary — a lightweight diary contact. This does NOT "
        "include them in proactive birthday/occasion reminders (that's a separate opt-in: "
        "in_scope, set with humans_update_person). Use when the user mentions someone not yet in the diary. "
        "Returns the new id; confirm to the user that you started a page for them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "e164": {"type": "string", "description": "Phone in E.164, optional."},
            "tier": {"type": "string", "enum": ["inner", "close", "extended"], "description": "default close"},
        },
        "required": ["name"],
    },
)
async def humans_add_person(args):
    _require_authenticated_reply()
    from src.brain.occasions import store

    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    p = store.add_person(name, e164=(args.get("e164") or None), tier=args.get("tier") or "close")
    return _ok(f"started a diary page for {p.name} (id={p.id}, tier={p.tier})")


@brain_tool(
    name="humans_log",
    description=(
        "Log a dated note about a person to their diary. Pick the section:\n"
        "- 'Timeline' (default) — what happened / was said (also bumps last_interaction)\n"
        "- 'Open threads' — a follow-up to circle back on ('job interview next week')\n"
        "- 'Profile' — a lasting fact ('works at Google, has 2 kids')\n"
        "- 'How I feel' / 'Inside jokes' / 'Shared memories' — relationship texture\n"
        "Resolve the person first (humans_find / humans_add_person) and pass person_id. "
        "Captures save directly to the vault — no approval needed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person_id": {"type": "string"},
            "note": {"type": "string"},
            "section": {
                "type": "string",
                "enum": ["Timeline", "Open threads", "Profile", "How I feel", "Inside jokes", "Shared memories"],
                "description": "default Timeline",
            },
        },
        "required": ["person_id", "note"],
    },
)
async def humans_log(args):
    _require_authenticated_reply()
    from src.brain.occasions import diary

    pid = (args.get("person_id") or "").strip()
    note = (args.get("note") or "").strip()
    section = args.get("section") or "Timeline"
    if not pid or not note:
        return _err("person_id and note are required")
    try:
        rel = diary.append_entry(pid, note, section=section)
    except ValueError as e:
        return _err(str(e))
    return _ok(f"logged to '{rel}' under ## {section}")


@brain_tool(
    name="humans_read",
    description=(
        "Read everything known about a person — registry facts (tier, number, "
        "occasions, last interaction, cadence) plus their full diary note (Profile, "
        "Timeline, Open threads, feelings). Use to recall before answering a question "
        "about them or drafting a message."
    ),
    input_schema={
        "type": "object",
        "properties": {"person_id": {"type": "string"}},
        "required": ["person_id"],
    },
)
async def humans_read(args):
    from src.brain.occasions import store
    from src.orchestrator.config import VAULT_PATH

    pid = (args.get("person_id") or "").strip()
    p = store.people_by_id().get(pid)
    if not p:
        return _err(f"no person '{pid}' in the diary")

    facts = [
        f"name: {p.name}",
        f"tier: {p.tier}",
        f"number: {p.e164 or '—'}",
        f"in_scope (proactive occasion reminders): {p.in_scope}",
        f"last_interaction: {p.last_interaction or '—'}",
        f"cadence: {store.effective_cadence(p) or 'off'}",
    ]
    rel = store.dossier_path(p)
    try:
        path = confined_path(VAULT_PATH, rel)
        note = read_regular_text(path)
    except FileNotFoundError:
        note = "(no diary note yet)"
    except (OSError, ValueError):
        return _err("dossier path escapes the vault")
    return _ok(_wrap_external(
        "PROFILE\n" + "\n".join(facts) + f"\n\nDIARY NOTE ({rel})\n" + note,
        source="humans/dossier",
    ))


@brain_tool(
    name="humans_due",
    description=(
        "List people you're overdue to reach out to — their stay-in-touch cadence "
        "(inner≈monthly, close≈quarterly, extended off; overridable per person) has "
        "lapsed since your last logged interaction. Returns name, tier, days since, "
        "cadence. People you've never logged an interaction with are NOT included "
        "(no baseline). Use in the daily pass to nudge gently."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def humans_due(args):
    from datetime import date

    from src.brain.occasions import store

    due = store.stay_in_touch_due(date.today())
    return _ok(_wrap_external(
        json.dumps({"overdue": due}, indent=2, ensure_ascii=False),
        source="humans/overdue",
    ))


@brain_tool(
    name="humans_open_threads",
    description=(
        "List every open follow-up across your diary (the '## Open threads' lines — "
        "things to circle back on, like 'ask Ravi how the interview went'). Returns "
        "person, thread, and a stable thread_id. Checked lines are excluded. Use "
        "thread_id with humans_resolve_thread so similar text cannot close the "
        "wrong follow-up."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def humans_open_threads(args):
    from src.brain.occasions import diary

    return _ok(_wrap_external(
        json.dumps(
            {"open_threads": diary.list_open_threads()},
            indent=2,
            ensure_ascii=False,
        ),
        source="humans/open-threads",
    ))


@brain_tool(
    name="humans_resolve_thread",
    description=(
        "Close one open follow-up by the exact thread_id returned from "
        "humans_open_threads. The line moves to '## Resolved threads' with a close "
        "date, preserving history. If outcome is supplied it is recorded there and "
        "in Timeline. `contains` is a legacy fallback and must match exactly one line."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person_id": {"type": "string"},
            "thread_id": {"type": "string", "description": "Exact id from humans_open_threads."},
            "contains": {"type": "string", "description": "Legacy unique-substring fallback."},
            "outcome": {"type": "string", "description": "Optional — what happened; logged to Timeline."},
        },
        "required": ["person_id"],
        "anyOf": [
            {"required": ["thread_id"]},
            {"required": ["contains"]},
        ],
    },
)
async def humans_resolve_thread(args):
    _require_authenticated_reply()
    from src.brain.occasions import diary

    pid = (args.get("person_id") or "").strip()
    thread_id = (args.get("thread_id") or "").strip()
    contains = (args.get("contains") or "").strip()
    if not pid or not (thread_id or contains):
        return _err("person_id and thread_id are required")
    try:
        ok = diary.resolve_thread(
            pid,
            contains=contains or None,
            thread_id=thread_id or None,
            outcome=(args.get("outcome") or None),
        )
    except ValueError as e:
        return _err(str(e))
    return _ok("resolved ✓" if ok else "no matching open thread found")


@brain_tool(
    name="humans_update_person",
    description=(
        "Update a person's attributes from chat: tier (inner/close/extended), "
        "in_scope (whether they get proactive occasion reminders — opt-in), cadence (stay-in-"
        "touch interval: weekly/fortnightly/monthly/quarterly/biannual/yearly, or "
        "'off'), or e164 (phone). Use for 'make Ravi inner', 'I want to send Ravi "
        "birthday messages too' (→ in_scope=true), 'check in with Anjali monthly'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person_id": {"type": "string"},
            "tier": {"type": "string", "enum": ["inner", "close", "extended"]},
            "in_scope": {"type": "boolean", "description": "opt into occasion reminders"},
            "cadence": {"type": "string", "description": "stay-in-touch interval, or 'off'"},
            "e164": {"type": "string", "description": "phone in E.164"},
        },
        "required": ["person_id"],
    },
)
async def humans_update_person(args):
    _require_authenticated_reply()
    from src.brain.occasions import store

    pid = (args.get("person_id") or "").strip()
    fields: dict = {}
    if args.get("tier"):
        fields["tier"] = args["tier"]
    if args.get("in_scope") is not None:
        fields["in_scope"] = bool(args["in_scope"])
    if args.get("cadence") is not None:
        c = str(args["cadence"]).strip().lower()
        fields["cadence"] = "" if c in ("off", "none", "") else c
    if args.get("e164"):
        fields["e164"] = str(args["e164"]).strip()
    if not pid or not fields:
        return _err("person_id and at least one field are required")
    p = store.update_person(pid, **fields)
    if not p:
        return _err(f"no person '{pid}' in the diary")
    return _ok(f"updated {p.name}: " + ", ".join(f"{k}={getattr(p, k)!r}" for k in fields))


@brain_tool(
    name="humans_set_occasion",
    description=(
        "Set an occasion for a person from chat. occasion='birthday' or "
        "'anniversary' with a date (YYYY-MM-DD, or --MM-DD if the year is unknown) "
        "sets and enables it. occasion=<festival id> (e.g. 'diwali') adds them to "
        "that festival (enabled=false removes). Use for 'Ravi's birthday is March 3' "
        "or 'remind me about Anjali at Diwali'. NOTE: proactive reminders only "
        "appear when the person is in_scope — set that with humans_update_person."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person_id": {"type": "string"},
            "occasion": {"type": "string", "description": "'birthday' | 'anniversary' | a festival id"},
            "date": {"type": "string", "description": "YYYY-MM-DD or --MM-DD (birthday/anniversary only)"},
            "enabled": {"type": "boolean", "description": "for festivals: false removes"},
        },
        "required": ["person_id", "occasion"],
    },
)
async def humans_set_occasion(args):
    _require_authenticated_reply()
    from src.brain.occasions import store

    pid = (args.get("person_id") or "").strip()
    occ = (args.get("occasion") or "").strip()
    if not pid or not occ:
        return _err("person_id and occasion are required")
    p = store.set_occasion(pid, occ, date=args.get("date"), enabled=args.get("enabled"))
    if not p:
        return _err(f"no person '{pid}' in the diary")
    note = "" if p.in_scope else " (note: not in_scope — set in_scope=true for proactive reminders)"
    return _ok(f"set {occ} for {p.name} ✓{note}")


@brain_tool(
    name="humans_meetings",
    description=(
        "Calendar events that involve people in your diary — for meeting-prep and "
        "smart-capture. scope='today'/'tomorrow' → UPCOMING: brief the user before "
        "they meet (humans_read each matched person). scope='yesterday' → PAST: ask "
        "how it went and capture any notes (humans_log). Returns events with matched "
        "person_ids (matched by name in the event title); confirm the match if unsure."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["today", "tomorrow", "yesterday"], "description": "default today"}
        },
    },
)
async def humans_meetings(args):
    from src.brain.occasions import calendar_people

    scope = args.get("scope") or "today"
    meetings = [
        {**meeting, "summary": _bracket(meeting.get("summary"))}
        for meeting in calendar_people.meetings(scope)
    ]
    return _ok(
        json.dumps(
            {"scope": scope, "meetings": meetings},
            indent=2,
            ensure_ascii=False,
        )
    )
