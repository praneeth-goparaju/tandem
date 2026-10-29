from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

ACTION_IDS = ("fu_details", "digest_show_done")
MAX_SHOWN = 8
STRANGER = "This is a private assistant."

_TEXT_LIMIT = 3000
_BUTTON_LIMIT = 75
_MODAL_TITLE_LIMIT = 24
_MODAL_BLOCK_LIMIT = 95
_OPEN_LOOPS_SHOWN = 5
_TODAY_SHOWN = 15

Block = dict[str, Any]


@dataclass(frozen=True)
class HomeData:
    needs_you: list[dict] = field(default_factory=list)
    today: list[str] = field(default_factory=list)
    health_line: str = ""
    weight_file_id: str | None = None
    open_loops: list[dict] = field(default_factory=list)
    open_count: int = 0
    handled: list[dict] = field(default_factory=list)


def _escape(text: object) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(text: str, limit: int = _BUTTON_LIMIT) -> dict:
    return {"type": "plain_text", "text": _cut(text, limit), "emoji": False}


def _section(text: str, **extra: Any) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cut(text, _TEXT_LIMIT)}, **extra}


def _button(label: str, action_id: str, value: str) -> Block:
    return {"type": "button", "text": _plain(label), "action_id": action_id, "value": value}


def _header(text: str) -> Block:
    return {"type": "header", "text": _plain(text, 150)}


def _first_line(text: object) -> str:
    return str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""


def _item_text(item: Mapping[str, Any]) -> str:
    text = _escape(item.get("summary"))
    note = _first_line(item.get("research"))
    if note and item.get("outcome") in {"found", "asked", "prepared"}:
        text += f"\n_{_escape(note)}_"
    return text


def _modal(title: str, blocks: list[Block], **extra: Any) -> dict:
    return {
        "type": "modal",
        "title": _plain(title, _MODAL_TITLE_LIMIT),
        "close": _plain("Close"),
        "blocks": blocks[:_MODAL_BLOCK_LIMIT] or [_section("Nothing here.")],
        **extra,
    }


def _proposal_line(proposal: Mapping[str, Any]) -> str:
    return f"• *{_escape(proposal.get('type'))}*: {_escape(proposal.get('rationale'))} `{_escape(proposal.get('id'))}`"


def details_modal(item: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> dict:
    blocks = [_section(f"*{_escape(item.get('summary'))}*")]
    if item.get("research"):
        blocks.append(_section(_escape(item["research"])))
    blocks += [_section(_proposal_line(action)) for action in actions]
    return _modal("Details", blocks)


def done_modal(handled: Sequence[Mapping[str, Any]]) -> dict:
    blocks = [_section(_item_text(item)) for item in handled]
    return _modal("Done while you slept", blocks)


def home_view(data: HomeData) -> dict:
    blocks: list[Block] = [_header("Needs you")]
    if data.needs_you:
        for item in data.needs_you[:MAX_SHOWN]:
            blocks.append(_section(
                _item_text(item),
                block_id=f"home_item_{item['id']}",
                accessory=_button("Details", "fu_details", str(item["id"])),
            ))
    else:
        blocks.append(_section("Nothing needs you right now."))
    blocks.append(_header("Today"))
    today = "\n".join(_escape(line) for line in data.today[:_TODAY_SHOWN])
    blocks.append(_section(today or "Nothing on the calendar or reminders today."))
    blocks.append(_header("Health"))
    blocks.append(_section(_escape(data.health_line) or "No Garmin data yet."))
    if data.weight_file_id:
        blocks.append({
            "type": "image",
            "slack_file": {"id": data.weight_file_id},
            "alt_text": "Weight, last 30 days",
            "title": _plain("Weight, last 30 days"),
        })
    blocks.append(_header("Open loops"))
    loops = "\n".join(f"• {_escape(item.get('summary'))}" for item in data.open_loops[:_OPEN_LOOPS_SHOWN])
    blocks.append(_section(f"{data.open_count} open" + (f"\n{loops}" if loops else "")))
    blocks.append(_header("Done overnight"))
    done: Block = _section(f"Done while you slept: {len(data.handled)}")
    if data.handled:
        done["accessory"] = _button("Show", "digest_show_done", "done")
    blocks.append(done)
    return {"type": "home", "blocks": blocks}


def home_attempts(view: dict) -> list[dict]:
    blocks = view.get("blocks", [])
    if not any(block.get("type") == "image" for block in blocks):
        return [view]
    return [view, {**view, "blocks": [block for block in blocks if block.get("type") != "image"]}]


def stranger_home_view() -> dict:
    return {"type": "home", "blocks": [_section(STRANGER)]}
