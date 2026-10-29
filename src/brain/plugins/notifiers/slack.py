from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from src.brain.actions.approval_review import build_approval_review
from src.brain.actions.outcomes import is_transient_exception
from src.brain.slack_text import sanitize_slack_text
from src.db import get_media

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

log = logging.getLogger("brain.plugins.notifier.slack")

FINDING_EVENT_TYPE = "brain_finding"
APPROVE_ACTION_ID = "brain_approve_action"
REJECT_ACTION_ID = "brain_reject_action"
_ACTION_ID_RE = re.compile(r"[a-f0-9]{12}", re.IGNORECASE)
_MAX_SLACK_BLOCKS = 50
_TRANSIENT_SLACK_ERRORS = frozenset({
    "fatal_error",
    "internal_error",
    "request_timeout",
    "service_unavailable",
})


def _slack_error_code(value: Any) -> str:
    response = getattr(value, "response", value)
    try:
        code = response.get("error")
    except (AttributeError, TypeError):
        code = None
    return str(code or "")


def _failed_delivery(
    error: Any,
    *,
    idempotency_key: str | None,
    transport_retryable: bool = False,
) -> dict[str, Any]:
    code = _slack_error_code(error)
    transient = transport_retryable or code in _TRANSIENT_SLACK_ERRORS
    result: dict[str, Any] = {
        "status": "failed",
        "id": None,
        "error": code or str(error),
    }
    if transient:
        if idempotency_key:
            result["retryable"] = True
        else:
            result["ambiguous"] = True
    return result


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![*_])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*_])")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
_MDLINK_RE = re.compile(r"(?<!!)\[([^\]\n]+?)\]\(([^\s)]+?)\)")
_HRULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^(\s*[-*]\s+)\[[ xX]\]\s+", re.MULTILINE)
_SLACK_CONTROL_RE = re.compile(r"<(?=[@#!])")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


_MD_MARKERS = frozenset("*[#-_")


_sanitize_outbound_text = sanitize_slack_text


def to_mrkdwn(text: str, vault_name: str | None = None, vault_quoted: str | None = None) -> str:
    """Convert a subset of CommonMark to Slack mrkdwn.

    Used for prose sections and for the `text` fallback. Messages with rules,
    headings or bullet lists are also sent as blocks — see `build_blocks`.

    Rules:
      **bold**            → *bold*
      *italic*            → _italic_     (only when not part of a ** pair)
      # H / ## H / …      → *H*          (Slack has no headings; bold is the closest)
      [text](url)         → <url|text>   (Slack link syntax)
      [[vault path]]      → <obsidian://open?vault=…&file=…|vault path>
                            (only if vault_name is configured)
      - [ ] / - [x]       → -            (drop checkbox — Slack renders literally)
      --- / *** / ___     → (stripped)   (horizontal rules render literally)
      leaves ~strike~ and `code` alone — those are identical syntax.
    """
    text = _sanitize_outbound_text(text)
    if not text:
        return text
    text = _SLACK_CONTROL_RE.sub("&lt;", text)
    if not any(c in text for c in _MD_MARKERS):
        return text
    placeholders: list[str] = []

    def _stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00B{len(placeholders) - 1}\x00"

    text = _HRULE_RE.sub("", text)
    text = _CHECKBOX_RE.sub(r"\1", text)

    if vault_name:
        v = vault_quoted if vault_quoted is not None else quote(vault_name, safe="")

        def _wiki(m: re.Match[str]) -> str:
            target = m.group(1).strip()
            explicit_label = m.group(2)
            if target.endswith(".md"):
                target = target[:-3]
            label = explicit_label.strip() if explicit_label else target
            path = quote(target, safe="/")
            return f"<obsidian://open?vault={v}&file={path}|{label}>"

        text = _WIKILINK_RE.sub(_wiki, text)

    def _mdlink(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        return f"<{url}|{label}>"

    text = _MDLINK_RE.sub(_mdlink, text)

    text = _HEADING_RE.sub(lambda m: _stash(m.group(1).strip()), text)
    stashed = _BOLD_RE.sub(lambda m: _stash(m.group(1)), text)
    italicized = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", stashed)
    for i, content in enumerate(placeholders):
        italicized = italicized.replace(f"\x00B{i}\x00", f"*{content}*")
    return _SLACK_CONTROL_RE.sub("&lt;", italicized)


# ---- Markdown structure → divider / rich_text blocks ----------------------
#
# Slack mrkdwn has no dividers, headings, or lists: a `---` line, a `## H`
# line and `- item` lines all render as literal text. Block Kit does have
# them, so a message that uses them is sent via the `blocks` field: a
# `divider` block for a rule, and `rich_text` blocks for headings (bold and
# underlined) and bullet lists. Prose in between stays a mrkdwn `section`.

_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+(\S.*)$")
_BULLET_LINE_RE = re.compile(r"^(\s*)[-•]\s+(?:\[[ xX]\]\s+)?(\S.*)$")
_FENCE_RE = re.compile(r"^\s*```")
_MAX_LIST_INDENT = 8
_RICH_LINK_SCHEMES = ("https://", "http://", "mailto:", "obsidian://")
_INLINE_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\[\[(?P<wiki>[^\]|]+?)(?:\|(?P<wikilabel>[^\]]+?))?\]\]"
    r"|(?<!!)\[(?P<label>[^\]\n]+?)\]\((?P<url>[^\s)]+?)\)"
    r"|<(?P<slackurl>https?://[^|>\s]+)(?:\|(?P<slacklabel>[^>\n]+))?>"
    r"|(?P<bare>https?://[^\s<>]+)"
    r"|(?<![*_])\*(?!\s)(?P<italic>[^*\n]+?)(?<!\s)\*(?![*_])"
    r"|(?<![\w_])_(?!\s)(?P<uitalic>[^_\n]+?)(?<!\s)_(?![\w_])"
    r"|(?<![\w~])~(?!\s)(?P<strike>[^~\n]+?)(?<!\s)~(?![\w~])"
)


def _rich_inline(
    text: str,
    style: dict[str, bool],
    vault_name: str | None,
    vault_quoted: str | None,
) -> list[dict[str, Any]]:
    """Turn one line of inline markdown into rich_text elements.

    Text elements are literal in Slack, so `<!channel>` and `<@U…>` written by
    a model stay plain characters here; no escaping is needed (or wanted).
    """
    elements: list[dict[str, Any]] = []

    def _text(value: str, **extra: bool) -> None:
        if not value:
            return
        element: dict[str, Any] = {"type": "text", "text": value}
        if style or extra:
            element["style"] = {**style, **extra}
        elements.append(element)

    def _link(url: str, label: str | None) -> None:
        element: dict[str, Any] = {"type": "link", "url": url}
        if label:
            element["text"] = label
        if style:
            element["style"] = dict(style)
        elements.append(element)

    def _nested(value: str, flag: str) -> None:
        elements.extend(
            _rich_inline(value, {**style, flag: True}, vault_name, vault_quoted)
        )

    pos = 0
    for m in _INLINE_RE.finditer(text):
        _text(text[pos : m.start()])
        pos = m.end()
        if m.group("code") is not None:
            _text(m.group("code"), code=True)
        elif m.group("bold") is not None:
            _nested(m.group("bold"), "bold")
        elif m.group("italic") is not None or m.group("uitalic") is not None:
            _nested(m.group("italic") or m.group("uitalic"), "italic")
        elif m.group("strike") is not None:
            _nested(m.group("strike"), "strike")
        elif m.group("wiki") is not None and vault_name:
            target = m.group("wiki").strip().removesuffix(".md")
            label = (m.group("wikilabel") or target).strip()
            v = vault_quoted if vault_quoted is not None else quote(vault_name, safe="")
            _link(f"obsidian://open?vault={v}&file={quote(target, safe='/')}", label)
        elif m.group("url") is not None and m.group("url").startswith(_RICH_LINK_SCHEMES):
            _link(m.group("url"), m.group("label").strip())
        elif m.group("slackurl") is not None:
            _link(m.group("slackurl"), m.group("slacklabel"))
        elif m.group("bare") is not None:
            url = m.group("bare").rstrip(".,;:!?)")
            _link(url, None)
            pos = m.start() + len(url)
        else:
            _text(m.group(0))
    _text(text[pos:])
    return elements


def _structure_blocks(
    lines: list[str],
    vault_name: str | None,
    vault_quoted: str | None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    prose: list[str] = []
    rich: list[dict[str, Any]] = []
    in_fence = False

    def _flush_prose() -> None:
        chunk = "\n".join(prose).strip()
        prose.clear()
        if chunk:
            blocks.extend(_text_blocks(to_mrkdwn(chunk, vault_name, vault_quoted)))

    def _flush_rich() -> None:
        if rich:
            blocks.append({"type": "rich_text", "elements": list(rich)})
            rich.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            _flush_rich()
            prose.append(line)
            continue
        if in_fence:
            prose.append(line)
            continue
        if _HRULE_RE.match(line):
            _flush_prose()
            _flush_rich()
            blocks.append({"type": "divider"})
            continue
        heading = _HEADING_LINE_RE.match(line)
        if heading:
            _flush_prose()
            _flush_rich()
            rich.append({
                "type": "rich_text_section",
                "elements": _rich_inline(
                    heading.group(1).strip(),
                    {"bold": True, "underline": True},
                    vault_name,
                    vault_quoted,
                ),
            })
            continue
        bullet = _BULLET_LINE_RE.match(line)
        if bullet:
            _flush_prose()
            indent = min(len(bullet.group(1).expandtabs(4)) // 2, _MAX_LIST_INDENT)
            item = {
                "type": "rich_text_section",
                "elements": _rich_inline(
                    bullet.group(2).strip(), {}, vault_name, vault_quoted
                ),
            }
            if rich and rich[-1]["type"] == "rich_text_list" and rich[-1]["indent"] == indent:
                rich[-1]["elements"].append(item)
            else:
                rich.append({
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": indent,
                    "elements": [item],
                })
            continue
        if not line.strip():
            if prose:
                prose.append(line)
            continue
        _flush_rich()
        prose.append(line)

    _flush_prose()
    _flush_rich()
    return blocks


# ---- Markdown tables → Block Kit `table` blocks ---------------------------
#
# Slack mrkdwn doesn't render tables — pipes appear literally. Block Kit's
# `table` block (released March 2026) does. When a message contains a
# markdown table we send via the `blocks` field instead of plain `text`.
# Multiple tables: only the first becomes a real table block (Slack allows
# one per message); the rest get bulletized into surrounding sections.


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _looks_like_table_row(line: str) -> bool:
    return "|" in line.strip() and not _TABLE_SEPARATOR_RE.match(line)


def _extract_md_tables(text: str) -> list[tuple[int, int, list[str], list[list[str]]]]:
    lines = text.splitlines()
    tables: list[tuple[int, int, list[str], list[list[str]]]] = []
    i = 0
    while i < len(lines):
        if (
            _TABLE_SEPARATOR_RE.match(lines[i])
            and i > 0
            and _looks_like_table_row(lines[i - 1])
        ):
            header_idx = i - 1
            j = i + 1
            while j < len(lines) and _looks_like_table_row(lines[j]):
                j += 1
            header = _split_row(lines[header_idx])
            rows = [_split_row(lines[k]) for k in range(i + 1, j)]
            tables.append((header_idx, j, header, rows))
            i = j
        else:
            i += 1
    return tables


def _table_to_block_kit(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    bk_rows = [[{"type": "raw_text", "text": cell} for cell in row] for row in [header, *rows]]
    return {"type": "table", "rows": bk_rows}


def _table_to_bullets(header: list[str], rows: list[list[str]]) -> str:
    out: list[str] = []
    for row in rows:
        parts = []
        for col, cell in enumerate(row):
            label = header[col] if col < len(header) else f"col{col + 1}"
            parts.append(f"{label}: {cell}")
        out.append("- " + " · ".join(parts))
    return "\n".join(out)


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _plain_section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


def _text_blocks(text: str, limit: int = 2900) -> list[dict[str, Any]]:
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return [_section(chunk) for chunk in chunks if chunk]


def _plain_text_blocks(
    text: str,
    limit: int = 2800,
    *,
    continuation_prefix: str = "",
) -> list[dict[str, Any]]:
    return [
        _plain_section(
            (continuation_prefix if offset else "") + text[offset : offset + limit]
        )
        for offset in range(0, len(text), limit)
    ]


def _pending_approval_rows(
    proposal_action_ids: list[str] | None,
    *,
    destination_area: str | None,
) -> list[dict[str, Any]]:
    if not proposal_action_ids or not destination_area:
        return []
    from src.brain.actions import queue

    ids: list[str] = []
    for candidate in proposal_action_ids:
        if not isinstance(candidate, str):
            continue
        action_id = candidate.strip().lower()
        if _ACTION_ID_RE.fullmatch(action_id) and action_id not in ids:
            ids.append(action_id)

    expected_audience = f"area:{destination_area}"
    rows: list[dict[str, Any]] = []
    for action_id in ids:
        row = queue.get(action_id)
        if (
            row
            and row.get("status") == "pending"
            and row.get("origin_area") == destination_area
            and row.get("approval_audience") == expected_audience
        ):
            rows.append(row)
        elif row and row.get("status") == "pending":
            log.warning(
                "approval card denied for action %s: destination_area=%r "
                "origin_area=%r audience=%r",
                action_id,
                destination_area,
                row.get("origin_area"),
                row.get("approval_audience"),
            )
    return rows


def _escape_mrkdwn(text: str) -> str:
    """Text Alfred's own code wrote, made safe for an mrkdwn field: no links, mentions or markup."""
    for old, new in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(old, new)
    return "".join(ch for ch in " ".join(text.split()) if ch not in "*_~`")


def _approval_blocks(
    rows: list[dict[str, Any]],
    *,
    max_blocks: int = _MAX_SLACK_BLOCKS,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for row in rows:
        action_id = str(row.get("id") or "")
        review = build_approval_review(row)
        remaining = max(0, max_blocks - len(blocks))
        if remaining == 0:
            break
        if not review.approvable:
            blocks.append(_plain_section(
                f"Approval disabled for action {action_id}: {review.reason}. "
                "Reject it and create a new reviewable proposal."
            ))
            continue

        body = "\n".join(
            [*([review.purpose, ""] if review.purpose else []), *review.summary_lines]
        ).strip()
        confirm_subject = (review.purpose or review.summary_lines[0])[:190]
        actions = {
            "type": "actions",
            "block_id": f"brain_approval_{action_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE_ACTION_ID,
                    "text": {
                        "type": "plain_text",
                        "text": "Approve",
                        "emoji": True,
                    },
                    "style": "primary",
                    "value": action_id,
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Approve this action?",
                        },
                        "text": {
                            "type": "plain_text",
                            "text": (
                                f"{review.heading}: {confirm_subject} "
                                f"(ref {action_id})"
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                        "style": "primary",
                    },
                },
                {
                    "type": "button",
                    "action_id": REJECT_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "style": "danger",
                    "value": action_id,
                },
            ],
        }
        card = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape_mrkdwn(review.title or review.heading)}*"}},
            *_plain_text_blocks(body, continuation_prefix="↳ continued: "),
            *([{"type": "context", "elements": [{"type": "mrkdwn", "text": _escape_mrkdwn(review.note)}]}]
              if review.note else []),
            actions,
        ]
        if len(card) > remaining:
            blocks.append(_plain_section(
                f"Approval controls omitted for action {action_id}: the complete "
                "review does not fit in this Slack message. Reject and re-propose "
                "it as a smaller action."
            ))
            continue
        blocks.extend(card)
    return blocks


def build_blocks(
    text: str,
    vault_name: str | None = None,
    vault_quoted: str | None = None,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Turn a markdown message into (fallback_text, blocks_or_none) for Slack.

    Prose only → returns (text, None) so callers send via the `text` field.
    Tables or markdown structure present → returns (original_text, blocks):
      --- on its own line  → `divider` block
      ## Heading           → `rich_text` section, bold and underlined
      - item / • item      → `rich_text` bullet list (consecutive lines)
      first table          → Block Kit `table` block; further tables become
                             bullet lists (Slack allows one table per message)
      everything else      → `section` blocks with mrkdwn
    The fallback text (returned as the first element) is what Slack uses for
    notifications and for clients that can't render the blocks.
    """
    text = _sanitize_outbound_text(text)
    if not text:
        return text, None

    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    cursor = 0
    used_table_block = False

    for start, end, header, rows in _extract_md_tables(text):
        blocks.extend(_structure_blocks(lines[cursor:start], vault_name, vault_quoted))
        if not used_table_block:
            blocks.append(_table_to_block_kit(header, rows))
            used_table_block = True
        else:
            bullets = _table_to_bullets(header, rows).splitlines()
            blocks.extend(_structure_blocks(bullets, vault_name, vault_quoted))
        cursor = end

    blocks.extend(_structure_blocks(lines[cursor:], vault_name, vault_quoted))

    if all(block["type"] == "section" for block in blocks):
        return text, None
    return text, blocks


def _get_agent_entry(name: str | None) -> dict | None:
    if not name:
        return None
    try:
        from src.orchestrator.registry import AGENT_REGISTRY
    except ImportError:
        return None
    return AGENT_REGISTRY.get(name)


def _registry_identity(name: str) -> dict | None:
    entry = _get_agent_entry(name)
    if not entry:
        return None
    identity = entry.get("identity")
    if not identity:
        return None
    return {
        "name": identity.get("display_name"),
        "icon_emoji": identity.get("icon_emoji"),
    }


def _untitled_media_label(media_id: str) -> str:
    row = get_media(media_id) or {}
    return "Chart" if row.get("kind") == "chart" else "Picture"


def _registry_channel_id(name: str) -> str | None:
    entry = _get_agent_entry(name)
    if not entry:
        return None
    channel_id = ((entry.get("delivery") or {}).get("slack") or {}).get("channel_id")
    if channel_id:
        return channel_id
    from src.orchestrator.chat import place_channel

    return place_channel("private")


class SlackNotifier:
    def __init__(
        self,
        bot_token: str | None = None,
        channels: dict[str, dict] | None = None,
        personas: dict[str, dict] | None = None,
        authorized_senders: dict | None = None,
        default_channel_id: str | None = None,
        vault_name: str | None = None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.channels = channels or {}
        self.personas = personas or {}
        self.vault_name = vault_name
        self._vault_quoted = quote(vault_name, safe="") if vault_name else None
        self._client: AsyncWebClient | None = None
        self._default_channel = (
            default_channel_id
            or (self.channels.get("default") or {}).get("channel_id")
        )

    def persona_for(self, area: str | None, agent: str | None = None) -> dict | None:
        """Return the persona dict ({name, icon_emoji}) for an area/agent, or None.

        Lookup order:
          1. Constructor-supplied `personas` dict (used by tests for isolation).
          2. Merged agent registry — read `agent.identity` from the agent named
             by `area` (or `agent` if `area` doesn't resolve).
        """
        if area and area in self.personas:
            return self.personas[area]
        if agent and agent in self.personas:
            return self.personas[agent]
        for name in (area, agent):
            if not name:
                continue
            identity = _registry_identity(name)
            if identity:
                return identity
        return None

    def channel_for_area(
        self, area: str | None, *, allow_default: bool = True,
    ) -> str | None:
        """Resolve an area's Slack channel, optionally falling back to default.

        Lookup order:
          1. Constructor-supplied `channels` dict (used by tests for isolation).
          2. Merged agent registry — read `agent.delivery.slack.channel_id`.
          3. `default_channel_id` (system-level fallback).
        """
        if area and area in self.channels:
            cid = self.channels[area].get("channel_id")
            if cid:
                return cid
        if area:
            cid = _registry_channel_id(area)
            if cid:
                return cid
        return self._default_channel if allow_default else None

    async def post_blocks(
        self, *, channel: str, text: str, blocks: list[dict],
        thread_ts: str | None = None, metadata: dict | None = None,
    ) -> str | None:
        extra: dict[str, Any] = {}
        if thread_ts:
            extra["thread_ts"] = thread_ts
        if metadata:
            extra["metadata"] = metadata
        try:
            response = await self._get_client().chat_postMessage(
                channel=channel, text=_sanitize_outbound_text(text), blocks=blocks, **extra,
            )
        except Exception as exc:
            log.warning("block message not posted to %s: %s", channel, _slack_error_code(exc) or exc)
            return None
        if hasattr(response, "get") and response.get("ok") is False:
            log.warning("block message refused in %s: %s", channel, _slack_error_code(response))
            return None
        ts = response.get("ts") if hasattr(response, "get") else None
        return str(ts) if ts else None

    async def post_media(
        self, media_ids: list[str], *, channel: str, thread_ts: str, title: str,
        area: str | None, agent: str | None, place: str | None,
    ) -> dict[str, Any]:
        return await self._post_media(
            media_ids, channel=channel, ts=thread_ts, thread_ts=thread_ts, title=title,
            area=area, agent=agent, place=place,
        )

    async def add_reaction(self, *, channel: str, ts: str, name: str) -> None:
        await self._get_client().reactions_add(channel=channel, timestamp=ts, name=name)

    async def remove_reaction(self, *, channel: str, ts: str, name: str) -> None:
        await self._get_client().reactions_remove(channel=channel, timestamp=ts, name=name)

    async def publish_home(self, *, user: str, view: dict) -> bool:
        from src.brain.followups.views import home_attempts

        for attempt in home_attempts(view):
            try:
                response = await self._get_client().views_publish(user_id=user, view=attempt)
            except Exception as exc:
                log.warning("home tab not published: %s", _slack_error_code(exc) or exc)
                continue
            if not (hasattr(response, "get") and response.get("ok") is False):
                return True
        return False

    async def upload_private_file(self, *, path: Path, title: str) -> str | None:
        response = await self._get_client().files_upload_v2(
            file=str(path), filename=path.name, title=title,
        )
        files = (response.get("files") if hasattr(response, "get") else None) or []
        single = response.get("file") if hasattr(response, "get") else None
        found = (files[0] if files else single) or {}
        file_id = found.get("id") if isinstance(found, dict) else None
        return str(file_id) if file_id else None

    def place_of_channel(self, channel: str) -> str | None:
        """The place whose chat agent is delivered to `channel`, as `channel_for_area` resolves it.

        None when no chat agent, or chat agents of both places, are delivered there.
        """
        from src.orchestrator.registry import AGENT_REGISTRY

        places = {
            entry["chat"] for name, entry in AGENT_REGISTRY.items()
            if entry.get("chat") and self.channel_for_area(name, allow_default=False) == channel
        }
        return places.pop() if len(places) == 1 else None

    def _get_client(self) -> AsyncWebClient:
        if self._client is None:
            from slack_sdk.web.async_client import AsyncWebClient

            self._client = AsyncWebClient(token=self.bot_token)
            self._client.proxy = None
        return self._client

    async def send(
        self,
        text: str,
        *,
        title: str = "",
        urgency: str = "info",
        finding_id: str | None = None,
        event_type: str | None = None,
        area: str | None = None,
        agent: str | None = None,
        quote_timestamp: int | str | None = None,
        idempotency_key: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        plain_text: bool = False,
        proposal_action_ids: list[str] | None = None,
        require_explicit_area: bool = False,
        media_ids: list[str] | None = None,
        place: str | None = None,
    ) -> dict[str, Any]:
        if not self.bot_token:
            log.warning("SLACK_BOT_TOKEN not configured — skipping Slack send")
            return {"status": "failed", "id": None, "error": "SLACK_BOT_TOKEN not set"}
        if media_ids:
            from src.brain.media.store import MAX_MEDIA_IDS

            if len(media_ids) > MAX_MEDIA_IDS:
                return {"status": "failed", "id": None, "error": f"at most {MAX_MEDIA_IDS} media ids"}

        approval_rows = (
            []
            if plain_text
            else _pending_approval_rows(
                proposal_action_ids,
                destination_area=area,
            )
        )
        channel = self.channel_for_area(
            area,
            allow_default=not approval_rows and not require_explicit_area,
        )
        if not channel:
            error = "no approval channel for area" if approval_rows else "no channel"
            log.warning("%s=%r — skipping Slack send", error, area)
            return {"status": "failed", "id": None, "error": error}

        structured_blocks = None
        if not plain_text:
            _, structured_blocks = build_blocks(
                text or "",
                vault_name=self.vault_name,
                vault_quoted=self._vault_quoted,
            )

        body_parts: list[str] = []
        safe_title = _sanitize_outbound_text(title)
        if not plain_text:
            safe_title = _SLACK_CONTROL_RE.sub("&lt;", safe_title)
        if safe_title:
            body_parts.append(safe_title if plain_text else f"*{safe_title}*")
        if text:
            body_parts.append(
                _sanitize_outbound_text(text)
                if plain_text
                else to_mrkdwn(
                    text,
                    vault_name=self.vault_name,
                    vault_quoted=self._vault_quoted,
                )
            )
        body = "\n\n".join(p for p in body_parts if p)
        persona = self.persona_for(area, agent) or {}
        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": body,
            "mrkdwn": not plain_text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if plain_text and body:
            kwargs["blocks"] = _plain_text_blocks(body)
        elif structured_blocks:
            blocks: list[dict[str, Any]] = []
            if safe_title:
                blocks.append(_section(f"*{safe_title}*"))
            blocks.extend(structured_blocks)
            if len(blocks) <= _MAX_SLACK_BLOCKS:
                kwargs["blocks"] = blocks
            else:
                # Slack rejects the whole message above the block limit; the
                # mrkdwn `text` body still carries every line.
                log.warning(
                    "Slack message needs %d blocks (limit %d) — sending as text",
                    len(blocks),
                    _MAX_SLACK_BLOCKS,
                )
        rendered_approval_ids: set[str] = set()
        if approval_rows:
            approval_blocks = _approval_blocks(
                approval_rows,
                max_blocks=_MAX_SLACK_BLOCKS - (1 if body else 0),
            )
            body_blocks = kwargs.get("blocks") or _text_blocks(body)
            kwargs["blocks"] = [
                *body_blocks[: _MAX_SLACK_BLOCKS - len(approval_blocks)],
                *approval_blocks,
            ]
            rendered_approval_ids = {
                block["block_id"].removeprefix("brain_approval_")
                for block in approval_blocks
                if str(block.get("block_id") or "").startswith("brain_approval_")
            }
        if attachments:
            kwargs["attachments"] = attachments
        if finding_id:
            kwargs["metadata"] = {
                "event_type": FINDING_EVENT_TYPE,
                "event_payload": {
                    "finding_id": finding_id,
                    "agent": agent or "",
                    "area": area or "",
                },
            }
        elif event_type:
            kwargs["metadata"] = {
                "event_type": event_type,
                "event_payload": {"agent": agent or "", "area": area or ""},
            }
        if persona.get("name"):
            kwargs["username"] = persona["name"]
        if persona.get("icon_emoji"):
            kwargs["icon_emoji"] = persona["icon_emoji"]
        elif persona.get("icon_url"):
            kwargs["icon_url"] = persona["icon_url"]
        if quote_timestamp:
            kwargs["thread_ts"] = str(quote_timestamp)
        if idempotency_key:
            kwargs["client_msg_id"] = idempotency_key

        try:
            resp = await self._get_client().chat_postMessage(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.error(f"Slack send failed: {e}")
            return _failed_delivery(
                e,
                idempotency_key=idempotency_key,
                transport_retryable=is_transient_exception(e),
            )

        if not resp.get("ok"):
            return _failed_delivery(resp, idempotency_key=idempotency_key)

        ts = resp.get("ts")
        log.info(
            "Slack sent area=%s channel=%s ts=%s chars=%d",
            area or "default",
            channel,
            ts,
            len(body),
        )
        if rendered_approval_ids and ts:
            from src.brain.actions import queue

            for row in approval_rows:
                action_id = str(row.get("id") or "")
                if action_id in rendered_approval_ids:
                    try:
                        queue.record_review_delivery(
                            action_id,
                            str(row.get("payload_hash") or ""),
                            area or "",
                            channel,
                            str(ts),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "approval review receipt failed for %s: %s",
                            action_id,
                            exc,
                        )
        result: dict[str, Any] = {
            "status": "sent",
            "id": ts,
            "urgency": urgency,
            "area": area,
            "channel_id": channel,
            "group_id": channel,
        }
        if media_ids:
            result.update(await self._post_media(
                media_ids, channel=channel, ts=ts,
                thread_ts=str(quote_timestamp) if quote_timestamp else ts,
                title=_sanitize_outbound_text(title), area=area, agent=agent, place=place,
            ))
        return result

    async def _post_media(
        self,
        media_ids: list[str],
        *,
        channel: str,
        ts: Any,
        thread_ts: Any,
        title: str,
        area: str | None,
        agent: str | None,
        place: str | None,
    ) -> dict[str, Any]:
        from src.brain.media.store import mark_sent, media_for_place

        if place is None and area:
            from src.orchestrator.chat import area_place

            place = area_place(area)
        destination = self.place_of_channel(channel)
        accepted, refused = media_for_place(media_ids, place if place == destination else None)
        failed: list[str] = []
        errors: dict[str, str] = {}
        for media_id, path in accepted:
            try:
                response = await self._get_client().files_upload_v2(
                    channel=channel, thread_ts=thread_ts, file=str(path),
                    filename=f"{media_id}.png", title=title or _untitled_media_label(media_id),
                )
                if hasattr(response, "get") and response.get("ok") is False:
                    raise RuntimeError(_slack_error_code(response) or "upload refused")
            except Exception as exc:
                log.warning("media %s was not uploaded: %s", media_id, exc)
                failed.append(media_id)
                errors[media_id] = (_slack_error_code(exc) or str(exc))[:100]
                continue
            try:
                mark_sent(media_id, agent=agent or "unknown", channel=channel, ts=str(ts))
            except Exception as exc:
                log.warning("media %s was posted but not recorded: %s", media_id, exc)
        outcome: dict[str, Any] = {}
        if refused:
            outcome["refused_media"] = refused
        if failed:
            outcome["failed_media"] = failed
            outcome["media_errors"] = errors
        return outcome
