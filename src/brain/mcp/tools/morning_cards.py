from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.brain.actions import queue
from src.brain.atomic_io import atomic_write_text
from src.brain.followups.cards import (
    MAX_LINE,
    MAX_OVERVIEW_LINE,
    MIN_LINE,
    Card,
    card_to_json,
    detail_blocks,
    parse_card,
    parse_overview,
    summary_blocks,
    summary_text,
)
from src.brain.followups.slack_history import MORNING_MESSAGE_EVENT
from src.brain.mcp._common import _bracket, _err, _ok, _wrap_external
from src.brain.mcp.caller_context import require_current_caller
from src.brain.mcp.tool_registry import brain_tool
from src.brain.mcp.tools.alfred import _channel_for_place, _chat_agent, _registry, _verification_run
from src.brain.notifications_audit import record_notification
from src.brain.slack_text import sanitize_slack_text
from src.db import get_follow_up, record_card, set_card_detail_ts
from src.db import handled_cards as db_handled_cards

PLACES = ("shared", "private")
UNLINKED_AREAS = frozenset({"discovery", "alfred_private"})
MAX_CARDS = 12
_FOLLOW_UP_ID = re.compile(r"F-[0-9]{6,}", re.IGNORECASE)

_DETAIL_SCHEMA = {
    "type": "object",
    "description": "The thread reply. Short: a heading, one quote line for the key fact, a few bullets, a source line.",
    "properties": {
        "heading": {"type": "string", "maxLength": 120},
        "quote": {"type": "string", "maxLength": 400},
        "bullets": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 8},
        "context": {"type": "string", "maxLength": 200, "description": "Where this came from, in a few words."},
    },
}


def _card_texts(card: Card) -> list[str]:
    d = card.detail
    return [card.title, card.line, d.heading, d.quote, d.context, *d.bullets, *card.asks,
            *(label for _, label in card.approvals)]


async def _today_agenda() -> tuple[str, ...]:
    """Today's calendar events and the reminders due today, in local time, written by code.

    The closing run does not list them itself, so nothing from another day can slip in.
    """
    import asyncio
    from datetime import date

    from src.brain.mcp.tools.icloud_read import reminders_due_on, today_events

    today = date.today()
    lines: list[tuple[str, str]] = []
    try:
        lines += await asyncio.to_thread(today_events, today)
    except Exception:  # noqa: BLE001
        lines.append(("", "Calendar could not be read"))
    try:
        lines += [(time, f"Reminder: {title}") for time, title in await asyncio.to_thread(reminders_due_on, today)]
    except Exception:  # noqa: BLE001
        lines.append(("", "Reminders could not be read"))
    if not lines:
        return ("Nothing in the calendar or reminders today",)
    order = sorted(lines, key=lambda line: ("0" if line[0] == "all day" else "1" if line[0] else "2", line[0]))
    return tuple(f"{time} {sanitize_slack_text(title)}".strip() for time, title in order)


def _check_place(place: str, cards: list[Card]) -> str | None:
    from src.orchestrator.chat import area_place

    for number, card in enumerate(cards, start=1):
        if not card.follow_up_id and card.area not in UNLINKED_AREAS:
            return (
                f"card {number}: every card about something open needs follow_up_id; "
                "reuse the item's id or create one with follow_ups_open"
            )
        if _FOLLOW_UP_ID.search(" ".join(_card_texts(card))):
            return f"card {number}: the text names a follow-up id; give it as follow_up_id and leave ids out of the text"
        if area_place(card.area) != place:
            return f"card {number}: area {_bracket(card.area)} does not post to the {place} channel"
        if card.follow_up_id:
            item = get_follow_up(card.follow_up_id.upper())
            if item is None:
                return f"card {number}: no follow-up {card.follow_up_id}"
            if place == "shared" and item["place"] != "shared":
                return f"card {number}: {card.follow_up_id} is a private follow-up"
    return None


def _screen(card: Card) -> tuple[Card, list[str]]:
    from dataclasses import replace

    kept, dropped = [], []
    for action_id, label in card.approvals:
        row = queue.get(action_id)
        if row is not None and row["status"] == "pending" and row["origin_area"] == card.area:
            kept.append((action_id, label))
        else:
            dropped.append(action_id)
    return replace(card, approvals=tuple(kept)), dropped


def _plain(card: Card) -> str:
    return summary_text(card).replace("*", "")


def _preview(cards: list[Card]) -> str:
    parts = []
    for card in cards:
        lines = [summary_text(card)]
        d = card.detail
        lines += [f"  *{d.heading}*"] if d.heading else []
        lines += [f"  > {d.quote}"] if d.quote else []
        lines += [f"  • {b}" for b in d.bullets]
        lines += [f"  _{d.context}_"] if d.context else []
        buttons = [label for _, label in card.approvals] + list(card.asks)
        if card.area:
            buttons += ["Done"] + (["Tomorrow", "Drop"] if card.follow_up_id else [])
        lines += [f"  [{'] ['.join(buttons)}]"] if buttons else []
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


async def _post_card(notifier: Any, card: Card, *, place: str, channel: str, chat_agent: str, agent: str) -> str | None:
    metadata = {"event_type": MORNING_MESSAGE_EVENT, "event_payload": {"agent": agent, "area": card.area or chat_agent}}
    summary_ts = await notifier.post_blocks(
        channel=channel, text=_plain(card), blocks=summary_blocks(card, status="open"), metadata=metadata,
    )
    if not summary_ts:
        return "the message was not posted"
    record_notification(
        agent=agent, area=card.area or chat_agent, title="(morning)", message=summary_text(card),
        status="sent", signal_ts=summary_ts, finding_id=None, group_id=channel,
    )
    card_id = record_card(
        place=place, channel=channel, summary_ts=summary_ts, detail_ts="", area=card.area or chat_agent,
        follow_up_id=card.follow_up_id.upper() if card.follow_up_id else None, card=card_to_json(card),
    )
    detail_ts = await notifier.post_blocks(
        channel=channel, text=card.detail.heading or _plain(card),
        blocks=detail_blocks(card, card_id=card_id, status="open"), thread_ts=summary_ts, metadata=metadata,
    )
    if not detail_ts:
        return "the detail reply was not posted"
    set_card_detail_ts(card_id, detail_ts)
    if card.media_ids:
        await notifier.post_media(
            list(card.media_ids), channel=channel, thread_ts=summary_ts, title=card.title,
            area=card.area, agent=agent, place=place,
        )
    return None


@brain_tool(
    name="send_morning_cards",
    description=(
        "Post the morning for one place as cards: `overview` first (one line such as "
        "'Saturday, 17 Oct. 3 things need you.', and a short detail with today, what was done "
        "overnight and health), then one card per thing that needs the owner. Each card is a "
        "message with an emoji, a bold title (a name or subject, 2 to 5 words) and a `line` of "
        "two or three plain sentences: what it is, why it matters today, and what to do "
        "(80 to 400 characters); its `detail` and buttons go in the thread. Buttons: every card gets Done; a card with "
        "`follow_up_id` also gets Tomorrow and Drop; `approvals` turns pending proposals of the "
        "card's area into buttons with your label; `asks` adds buttons that ask you something "
        "in the thread when tapped (for example 'Draft wishes'); approvals and asks together "
        "add at most 2. Every card's area must belong to the place, and the shared place takes "
        "only shared follow-ups; otherwise nothing is sent. Call once per place. Slack-style "
        "markdown: **bold**, links; no tables, no ids or internal names in the text."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "place": {"type": "string", "enum": list(PLACES)},
            "overview": {
                "type": "object",
                "properties": {"line": {"type": "string", "maxLength": MAX_OVERVIEW_LINE}, "detail": _DETAIL_SCHEMA},
                "required": ["line"],
            },
            "cards": {
                "type": "array",
                "maxItems": MAX_CARDS,
                "items": {
                    "type": "object",
                    "properties": {
                        "area": {"type": "string"},
                        "emoji": {"type": "string", "maxLength": 16, "description": "One emoji, such as 🎂 or 📤."},
                        "title": {"type": "string", "maxLength": 60},
                        "line": {
                            "type": "string", "minLength": MIN_LINE, "maxLength": MAX_LINE,
                            "description": "Two or three plain sentences: what it is, why it matters today, what to do.",
                        },
                        "detail": _DETAIL_SCHEMA,
                        "follow_up_id": {"type": "string", "pattern": "^F-[0-9]{6,}$"},
                        "approvals": {
                            "type": "array", "maxItems": 2,
                            "items": {"type": "object", "properties": {
                                "id": {"type": "string", "pattern": "^[a-fA-F0-9]{12}$"},
                                "label": {"type": "string", "maxLength": 40},
                            }, "required": ["id", "label"]},
                        },
                        "asks": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 40}},
                        "media_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["area", "emoji", "title", "line"],
                },
            },
        },
        "required": ["place", "overview", "cards"],
    },
)
async def send_morning_cards(args):
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    if caller.is_reply or (_registry().get(caller.agent) or {}).get("chat") != "private":
        return _err("send_morning_cards is only available to the closing run")
    place = args.get("place")
    if place not in PLACES:
        return _err(f"place must be one of {', '.join(PLACES)}")
    raw_cards = args.get("cards")
    if not isinstance(raw_cards, list) or len(raw_cards) > MAX_CARDS:
        return _err(f"cards must be a list of at most {MAX_CARDS}")
    try:
        overview = parse_overview(args.get("overview"))
        cards = []
        for number, raw in enumerate(raw_cards, start=1):
            try:
                cards.append(parse_card(raw))
            except ValueError as exc:
                raise ValueError(f"card {number}: {exc}") from exc
    except ValueError as exc:
        return _err(f"{exc}; nothing was sent")
    refusal = _check_place(place, cards)
    if refusal:
        return _err(f"{refusal}; nothing was sent")
    chat_agent = _chat_agent(place)
    channel = _channel_for_place(place)
    if chat_agent is None or channel is None:
        return _err(f"no chat agent with a Slack channel is configured for the {place} place")
    dropped: list[str] = []
    screened = []
    for card in cards:
        kept, lost = _screen(card)
        screened.append(kept)
        dropped += lost
    dropped_note = (
        "\nDropped approval ids (not pending, or from another area): " + ", ".join(dropped) if dropped else ""
    )
    if place == "private":
        overview = replace(overview, detail=replace(
            overview.detail, bullets=(*await _today_agenda(), *overview.detail.bullets),
        ))
    if _verification_run():
        from src.orchestrator.config import MEMORY_DB_PATH

        path = Path(MEMORY_DB_PATH).parent / f"morning-{place}.md"
        atomic_write_text(path, _preview([overview, *screened]))
        return _ok(f"Verification run: nothing was sent. Wrote {len(screened)} cards to {path}.{dropped_note}")

    from src.brain.mcp._common import notifier

    slack = notifier()
    failed = []
    for number, card in enumerate([overview, *screened]):
        error = await _post_card(slack, card, place=place, channel=channel, chat_agent=chat_agent, agent=caller.agent)
        if error:
            failed.append(f"{'overview' if number == 0 else f'card {number}'}: {error}")
            if number == 0:
                return _err(f"the {place} overview was not posted ({error}); nothing else was sent")
    failed_note = "\nNot posted: " + "; ".join(failed) if failed else ""
    return _ok(f"Posted the {place} morning: the overview and {len(screened) - len(failed)} cards.{failed_note}{dropped_note}")


HANDLED_DAYS = 14
_HANDLED_WORDS = {"done": "Done", "dropped": "Dropped", "later": "Tomorrow"}


def _start_of_today_utc(now: datetime) -> str:
    local = now.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


@brain_tool(
    name="handled_cards",
    description=(
        f"The morning cards the owner answered in the last {HANDLED_DAYS} days with Done or Drop, "
        "and the ones put off with Tomorrow today. Do not post a card about the same subject again "
        "unless something new happened since, and then say what is new."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def handled_cards(args):
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    if caller.is_reply or (_registry().get(caller.agent) or {}).get("chat") != "private":
        return _err("handled_cards is only available to the closing run")
    now = datetime.now(UTC)
    today = _start_of_today_utc(now)
    since = (now - timedelta(days=HANDLED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    lines = []
    for row in db_handled_cards(since):
        if row["status"] == "later" and row["created"] < today:
            continue
        card = row["card"]
        lines.append(
            f"{row['created'][:10]} | {row['place']} | {_HANDLED_WORDS[row['status']]} | "
            f"{card.get('title', '')}: {card.get('line', '')}"
        )
    if not lines:
        return _ok("No cards were marked Done, Tomorrow or Drop.")
    return _ok(_wrap_external("\n".join(lines), source="handled_cards"))
