from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.brain.plugins.notifiers.slack import to_mrkdwn
from src.brain.slack_text import sanitize_slack_text

CARD_ACTIONS = ("card_done", "card_later", "card_drop", "card_approve", "card_ask")
OVERVIEW_EMOJI = "☀️"
STATUS_LINES = {"done": "✅ Done", "later": "⏰ Tomorrow", "dropped": "✖️ Dropped"}

MAX_TITLE = 60
MIN_LINE = 80
MAX_LINE = 400
MAX_OVERVIEW_LINE = 200
MAX_HEADING = 120
MAX_QUOTE = 400
MAX_BULLET = 200
MAX_BULLETS = 8
MAX_CONTEXT = 200
MAX_EMOJI = 16
MAX_LABEL = 40
MAX_EXTRA_BUTTONS = 2

_FOLLOW_UP_ID = re.compile(r"F-[0-9]{6,}")
_ACTION_ID = re.compile(r"[a-f0-9]{12}")

Block = dict[str, Any]


@dataclass(frozen=True)
class Detail:
    heading: str = ""
    quote: str = ""
    bullets: tuple[str, ...] = ()
    context: str = ""


@dataclass(frozen=True)
class Card:
    area: str
    emoji: str
    title: str
    line: str
    detail: Detail = field(default_factory=Detail)
    follow_up_id: str | None = None
    approvals: tuple[tuple[str, str], ...] = ()
    asks: tuple[str, ...] = ()
    media_ids: tuple[str, ...] = ()


def _text(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        need = f"1 to {limit}" if required else f"at most {limit}"
        raise ValueError(f"{name} must be text of {need} characters")
    return " ".join(sanitize_slack_text(value).split())


def _detail(raw: object) -> Detail:
    if raw is None:
        return Detail()
    if not isinstance(raw, Mapping):
        raise ValueError("detail must be an object with heading, quote, bullets and context")
    bullets = raw.get("bullets") or []
    if not isinstance(bullets, list) or len(bullets) > MAX_BULLETS:
        raise ValueError(f"bullets must be a list of at most {MAX_BULLETS} lines")
    return Detail(
        heading=_text(raw.get("heading"), "heading", MAX_HEADING),
        quote=_text(raw.get("quote"), "quote", MAX_QUOTE),
        bullets=tuple(_text(b, "bullets", MAX_BULLET, required=True) for b in bullets),
        context=_text(raw.get("context"), "context", MAX_CONTEXT),
    )


def _sentences(raw: object) -> str:
    text = _text(raw, "line", MAX_LINE, required=True)
    if len(text) < MIN_LINE:
        raise ValueError(
            f"line must be two or three plain sentences ({MIN_LINE} to {MAX_LINE} characters): "
            "what it is, why it matters today, what to do"
        )
    return text


def _approvals(raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("approvals must be a list of {id, label}")
    found = []
    for entry in raw:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("id"), str)
            or not _ACTION_ID.fullmatch(entry["id"].strip().lower())
        ):
            raise ValueError("approvals: each needs the 12-character id of a pending proposal")
        found.append((entry["id"].strip().lower(), _text(entry.get("label"), "approvals label", MAX_LABEL, required=True)))
    return tuple(found)


def parse_card(raw: object) -> Card:
    """Check one card from the closing run; ValueError names the field that is wrong."""
    if not isinstance(raw, Mapping):
        raise ValueError("each card must be an object")
    follow_up_id = raw.get("follow_up_id")
    if follow_up_id is not None and (not isinstance(follow_up_id, str) or not _FOLLOW_UP_ID.fullmatch(follow_up_id)):
        raise ValueError("follow_up_id must look like F-000012")
    asks = raw.get("asks") or []
    if not isinstance(asks, list):
        raise ValueError("asks must be a list of short button labels")
    card = Card(
        area=_text(raw.get("area"), "area", 60, required=True),
        emoji=_text(raw.get("emoji"), "emoji", MAX_EMOJI, required=True),
        title=_text(raw.get("title"), "title", MAX_TITLE, required=True),
        line=_sentences(raw.get("line")),
        detail=_detail(raw.get("detail")),
        follow_up_id=follow_up_id,
        approvals=_approvals(raw.get("approvals")),
        asks=tuple(_text(a, "asks", MAX_LABEL, required=True) for a in asks),
        media_ids=tuple(raw.get("media_ids") or ()),
    )
    if len(card.approvals) + len(card.asks) > MAX_EXTRA_BUTTONS:
        raise ValueError(f"asks and approvals together may add at most {MAX_EXTRA_BUTTONS} buttons")
    return card


def parse_overview(raw: object) -> Card:
    if not isinstance(raw, Mapping):
        raise ValueError("overview must be an object with line and detail")
    line = _text(raw.get("line"), "overview line", MAX_OVERVIEW_LINE, required=True)
    title, _, rest = line.partition(". ")
    return Card(area="", emoji=OVERVIEW_EMOJI, title=title.rstrip("."), line=rest, detail=_detail(raw.get("detail")))


def summary_text(card: Card) -> str:
    if _is_overview(card):
        return f"{OVERVIEW_EMOJI} *{to_mrkdwn(card.title)}.* {to_mrkdwn(card.line)}".strip()
    return f"{card.emoji} *{to_mrkdwn(card.title)}*\n{to_mrkdwn(card.line)}"


def _section(text: str, **extra: Any) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}, **extra}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def _button(label: str, action_id: str, value: str, *, primary: bool = False) -> Block:
    button: Block = {
        "type": "button", "text": {"type": "plain_text", "text": label[:75], "emoji": True},
        "action_id": action_id, "value": value,
    }
    if primary:
        button["style"] = "primary"
    return button


def summary_blocks(card: Card, *, status: str) -> list[Block]:
    blocks = [_section(summary_text(card))]
    if status in STATUS_LINES:
        blocks.append(_context(STATUS_LINES[status]))
    return blocks


def _is_overview(card: Card) -> bool:
    return not card.area


def detail_blocks(
    card: Card, *, card_id: int, status: str, approved: set[str] | frozenset[str] = frozenset(),
) -> list[Block]:
    parts = []
    if card.detail.heading:
        parts.append(f"*{to_mrkdwn(card.detail.heading)}*")
    if card.detail.quote:
        parts.append("> " + to_mrkdwn(card.detail.quote))
    if card.detail.bullets:
        parts.append("\n".join(f"• {to_mrkdwn(b)}" for b in card.detail.bullets))
    blocks = [_section("\n".join(parts) or summary_text(card))]
    if card.detail.context:
        blocks.append(_context(to_mrkdwn(card.detail.context)))
    for action_id, label in card.approvals:
        if action_id in approved:
            blocks.append(_context(f"✅ Approved: {to_mrkdwn(label)}"))
    if status in STATUS_LINES:
        blocks.append(_context(STATUS_LINES[status]))
        return blocks
    if _is_overview(card):
        return blocks
    buttons = [
        _button(label, "card_approve", f"{card_id}:{index}")
        for index, (action_id, label) in enumerate(card.approvals) if action_id not in approved
    ]
    buttons += [_button(label, "card_ask", f"{card_id}:{index}") for index, label in enumerate(card.asks)]
    buttons.append(_button("✅ Done", "card_done", str(card_id)))
    if card.follow_up_id:
        buttons += [_button("⏰ Tomorrow", "card_later", str(card_id)), _button("✖️ Drop", "card_drop", str(card_id))]
    buttons[0]["style"] = "primary"
    blocks.append({"type": "actions", "block_id": f"card_{card_id}", "elements": buttons})
    return blocks


def card_to_json(card: Card) -> dict:
    return {
        "area": card.area, "emoji": card.emoji, "title": card.title, "line": card.line,
        "detail": {"heading": card.detail.heading, "quote": card.detail.quote,
                   "bullets": list(card.detail.bullets), "context": card.detail.context},
        "follow_up_id": card.follow_up_id,
        "approvals": [{"id": i, "label": label} for i, label in card.approvals],
        "asks": list(card.asks),
    }


def card_from_json(stored: Mapping[str, Any]) -> Card:
    detail = stored.get("detail") or {}
    return Card(
        area=stored.get("area") or "", emoji=stored.get("emoji") or "", title=stored.get("title") or "",
        line=stored.get("line") or "",
        detail=Detail(detail.get("heading") or "", detail.get("quote") or "",
                      tuple(detail.get("bullets") or ()), detail.get("context") or ""),
        follow_up_id=stored.get("follow_up_id"),
        approvals=tuple((a["id"], a["label"]) for a in stored.get("approvals") or ()),
        asks=tuple(stored.get("asks") or ()),
    )
