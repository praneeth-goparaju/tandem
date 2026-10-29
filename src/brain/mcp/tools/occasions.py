from __future__ import annotations

import json

from src.brain.mcp._common import _ok, _wrap_external
from src.brain.mcp.caller_context import require_current_caller
from src.brain.mcp.tool_registry import brain_tool

INTERVIEW_LEAD_DAYS = 5
DRAFT_LEAD_DAYS = 1
DEFAULT_WINDOW_DAYS = 7


@brain_tool(
    name="occasions_scan",
    description=(
        "Scan upcoming birthdays / anniversaries / festivals for the people in "
        "people.yaml and return the ones within `days` (default 7), soonest first. "
        "Each entry: person_id, person_name, occasion ('birthday'|'anniversary'|"
        "festival id), date, days_out, year, already_reminded, interview_due (≈5 days "
        "out — ask the user for fresh notes), draft_due (≈1 day out — draft + "
        "include suggested wording in Slack). Only in-scope people are included; "
        "already-reminded "
        "occasions are returned with their flags false so you can see the full "
        "picture. Run this first every cycle."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": f"Look-ahead window. Default {DEFAULT_WINDOW_DAYS}."}
        },
    },
)
async def occasions_scan(args):
    from src.brain.occasions import occasion_guard as og
    from src.brain.occasions import store

    days = int(args.get("days") or DEFAULT_WINDOW_DAYS)
    today = store.local_date()
    pf = store.load_people()
    by_id = {p.id: p for p in pf.people}
    festivals = store.load_festivals().festivals

    out = []
    for h in og.window_hits(pf.people, festivals, today, days):
        occ_year = int(h["date"][:4])
        already = store.already_reminded(h["person_id"], h["occasion"], occ_year)
        d = h["days_out"]
        p = by_id.get(h["person_id"])
        out.append(
            {
                **h,
                "year": occ_year,
                "already_reminded": already,
                "interview_due": (not already and DRAFT_LEAD_DAYS < d <= INTERVIEW_LEAD_DAYS),
                "draft_due": (not already and d <= DRAFT_LEAD_DAYS),
                "tier": p.tier if p else None,
                "dossier": store.dossier_path(p) if p else None,
                "notes": p.notes if p else "",
            }
        )

    voice = store.load_voice()
    return _ok(
        json.dumps(
            {
                "today": today.isoformat(),
                "window_days": days,
                "voice": {
                    "profile": voice.profile.model_dump(),
                    "banned_cliches": voice.banned_cliches,
                },
                "occasions": out,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@brain_tool(
    name="occasions_record_reminder",
    description=(
        "Verify that the successful humans-area Slack notification already produced "
        "a server-recorded receipt for this person/occasion/year. The notification "
        "handler records it only after the complete reminder is delivered."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person_id": {"type": "string"},
            "occasion": {"type": "string", "description": "'birthday'|'anniversary'|festival id"},
            "year": {"type": "integer"},
        },
        "required": ["person_id", "occasion", "year"],
    },
)
async def occasions_record_reminder(args):
    from src.brain.occasions import store

    pid = (args.get("person_id") or "").strip()
    occ = (args.get("occasion") or "").strip()
    year = args.get("year")
    if not (pid and occ and year is not None):
        return _ok("ERROR: person_id, occasion, and year are all required")
    if pid not in store.people_by_id():
        return _ok(f"ERROR: no person '{pid}' in people.yaml")
    if not store.already_reminded(pid, occ, int(year)):
        return _ok(
            "ERROR: no server-verified successful Slack reminder receipt for "
            f"{pid} {occ}:{int(year)}"
        )
    return _ok(f"verified Slack reminder receipt: {pid} {occ}:{int(year)}")


@brain_tool(
    name="occasions_get_dossier",
    description=(
        "Read a person's dossier note — their private profile (how the user feels "
        "about them, inside jokes, shared memories, life updates, last year's "
        "message). Pass person_id. Returns the markdown, or a note that no dossier "
        "exists yet. This is what makes a draft genuinely personal — read it before "
        "drafting."
    ),
    input_schema={
        "type": "object",
        "properties": {"person_id": {"type": "string"}},
        "required": ["person_id"],
    },
)
async def occasions_get_dossier(args):
    from src.brain.atomic_io import confined_path, read_regular_text
    from src.brain.occasions import store
    from src.orchestrator.config import VAULT_PATH

    pid = (args.get("person_id") or "").strip()
    p = store.people_by_id().get(pid)
    if not p:
        return _ok(f"ERROR: no person '{pid}' in people.yaml")

    rel = store.dossier_path(p)
    try:
        path = confined_path(VAULT_PATH, rel)
    except ValueError:
        return _ok("ERROR: dossier path escapes the vault or crosses a symlink")
    try:
        return _ok(_wrap_external(
            read_regular_text(path),
            source="occasions/dossier",
        ))
    except FileNotFoundError:
        return _ok(f"(no dossier note yet for {p.name} — create it at '{rel}')")
    except Exception as e:  # noqa: BLE001
        return _ok(f"ERROR reading dossier: {type(e).__name__}: {e}")


@brain_tool(
    name="occasions_learn_edit",
    description=(
        "Record that the user edited one of your drafts before sending. Captures "
        "the draft→final diff into the voice profile so future drafts sound more "
        "like them, and promotes a phrasing to the banned-cliché list once it's "
        "been cut in two or more edits. Call this whenever the user replies with an "
        "edited version of a message you drafted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft": {"type": "string", "description": "The text you originally drafted."},
            "final": {"type": "string", "description": "What the user actually sent / their edit."},
        },
        "required": ["draft", "final"],
    },
)
async def occasions_learn_edit(args):
    caller = require_current_caller()
    if not caller.is_reply:
        raise PermissionError("voice learning is available only during an authenticated reply")
    from src.brain.occasions import learning

    draft = args.get("draft") or ""
    final = args.get("final") or ""
    if not final.strip():
        return _ok("ERROR: final text is required")
    learned = learning.record_edit(draft, final)
    return _ok("recorded edit — voice profile updated" if learned else "no change (identical)")
