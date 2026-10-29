from __future__ import annotations

import logging
from datetime import date, timedelta

from src.brain.followups.cards import card_from_json, detail_blocks, summary_blocks, summary_text
from src.brain.followups.close import close_follow_up
from src.db import (
    get_card,
    set_card_payload,
    set_card_status,
    snooze_follow_up,
)

log = logging.getLogger("brain.card_actions")

OWNER_ONLY = "Only the owner can use these buttons."
NOT_ON_MESSAGE = "That item is not on this message."
NOT_ALLOWED = "These buttons are not yours to use."
_STATUS = {"card_done": "done", "card_later": "later", "card_drop": "dropped"}


def _allowed(card: dict, identity: dict | None) -> str | None:
    if identity is None:
        return OWNER_ONLY
    if card["place"] == "private":
        return None if identity.get("is_owner") is True else OWNER_ONLY
    from src.orchestrator.chat import place_areas
    from src.orchestrator.reply_routing import sender_allowed_for_place

    return None if sender_allowed_for_place(identity, place_areas("shared"), whole_vault=False) else NOT_ALLOWED


def _redraw(card: dict) -> dict:
    stored = card["card"]
    model = card_from_json(stored)
    approved = set(stored.get("approved") or ())
    return {"updates": [
        {"channel": card["channel"], "ts": card["summary_ts"], "text": summary_text(model).replace("*", ""),
         "blocks": summary_blocks(model, status=card["status"])},
        {"channel": card["channel"], "ts": card["detail_ts"], "text": model.detail.heading or model.title,
         "blocks": detail_blocks(model, card_id=card["id"], status=card["status"], approved=approved)},
    ]}


async def _finish(card: dict, action: str, today: str) -> None:
    item = card.get("follow_up_id")
    if item:
        if action == "card_done":
            await close_follow_up(item, "done", actor="owner")
        elif action == "card_later":
            snooze_follow_up(item, until=(date.fromisoformat(today) + timedelta(days=1)).isoformat())
        elif action == "card_drop":
            await close_follow_up(item, "dismissed", actor="owner")
    set_card_status(card["id"], _STATUS[action])


async def act(msg: dict, identity: dict | None, *, today: str) -> dict:
    """Do what one card button asks; return the message updates or a private notice."""
    from src.orchestrator import reply_routing

    channel = str(msg.get("group_id") or "")
    action = str(msg.get("action") or "")
    raw_id, _, raw_index = str(msg.get("value") or "").partition(":")
    card = get_card(int(raw_id)) if raw_id.isdigit() else None
    if card is None or card["channel"] != channel or card["detail_ts"] != str(msg.get("target_timestamp") or ""):
        return {"ephemeral": {"channel": channel, "text": NOT_ON_MESSAGE}}
    refusal = _allowed(card, identity)
    if refusal:
        return {"ephemeral": {"channel": channel, "text": refusal}}
    sender = str(msg.get("sender") or "")
    stored = card["card"]

    if action in _STATUS:
        if card["status"] == "open":
            await _finish(card, action, today)
        return _redraw(get_card(card["id"]) or card)

    index = int(raw_index) if raw_index.isdigit() else -1
    unique = f"card:{card['id']}:{action}:{index}:{msg.get('timestamp') or ''}"
    if action == "card_approve":
        approvals = stored.get("approvals") or []
        if not 0 <= index < len(approvals):
            return {}
        action_id = approvals[index]["id"]
        outcome = await reply_routing.on_reply({
            "type": "approval", "decision": "approved", "action_id": action_id, "sender": sender,
            "group_id": channel, "timestamp": unique, "target_timestamp": card["detail_ts"],
        }) or {}
        if outcome.get("outcome") in ("approved", "already_resolved"):
            stored = {**stored, "approved": sorted({*(stored.get("approved") or ()), action_id})}
            set_card_payload(card["id"], stored)
            return _redraw({**card, "card": stored})
        return {"ephemeral": {"channel": channel, "text": "That approval did not go through. Nothing ran."}}
    if action == "card_ask":
        asks = stored.get("asks") or []
        if not 0 <= index < len(asks):
            return {}
        await reply_routing.on_reply({
            "type": "reply", "text": asks[index], "sender": sender, "group_id": channel,
            "channel_type": "channel", "timestamp": unique, "quoted_timestamp": card["summary_ts"],
            "reply_thread_ts": card["summary_ts"],
            "quoted_text": summary_text(card_from_json(stored)), "quoted_from_bot": True,
            "finding_id": None, "images": [], "image_count": 0,
        })
        return {}
    return {}


