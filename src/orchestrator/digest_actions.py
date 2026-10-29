from __future__ import annotations

import logging
import os
from datetime import date, datetime

from src.brain.actions import queue
from src.brain.followups.morning import latest_night, needs_and_handled
from src.brain.followups.views import ACTION_IDS, details_modal, done_modal
from src.db import follow_ups_by_ids, get_follow_up

log = logging.getLogger("brain.digest_actions")

__all__ = ["ACTION_IDS", "handle"]

_OPEN = ("open", "raised")


def _local_today() -> str:
    return datetime.now().date().isoformat()


def _is_owner(identity: dict | None) -> bool:
    return identity is not None and identity.get("is_owner") is True


def _details(item_id: str, trigger_id: str) -> dict:
    found = follow_ups_by_ids([item_id])
    if not found:
        return {}
    actions = [proposal for action_id in found[0]["action_ids"]
               if (proposal := queue.get(action_id)) is not None]
    return {"modal": details_modal(found[0], actions), "trigger_id": trigger_id}


def _on_home(action: str, item_id: str, trigger_id: str) -> dict:
    if action == "fu_details":
        item = get_follow_up(item_id)
        return _details(item_id, trigger_id) if item and item["status"] in _OPEN else {}
    if action == "digest_show_done":
        today = date.fromisoformat(_local_today())
        night = latest_night(today)
        return {"modal": done_modal(needs_and_handled(night, today)[1]), "trigger_id": trigger_id}
    return {}


def _act(msg: dict, identity: dict | None) -> dict:
    if msg.get("container") != "view" or not _is_owner(identity):
        return {}
    return _on_home(
        str(msg.get("action") or ""), str(msg.get("value") or ""), str(msg.get("trigger_id") or ""),
    )


async def handle(msg: dict, identity: dict | None) -> dict:
    """Act on one card or Home tab click; return what Slack should show.

    A click that changed a message also refreshes the owner's Home tab.
    """
    if msg.get("type") == "card":
        from src.orchestrator import card_actions

        result = await card_actions.act(msg, identity, today=_local_today())
    else:
        result = _act(msg, identity)
    owner = os.environ.get("OWNER_SLACK_USER_ID", "").strip()
    if ("update" in result or "updates" in result) and owner and _is_owner(identity):
        from src.brain.followups.home import build_home_view
        from src.brain.mcp._common import notifier

        try:
            view = await build_home_view(notifier())
        except Exception as exc:  # noqa: BLE001
            log.warning("home tab not rebuilt after a click: %s", exc)
        else:
            result["home"] = {"user": owner, "view": view}
    return result
