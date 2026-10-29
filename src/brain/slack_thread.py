from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("brain.slack_thread")

_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_DEFAULT_TTL = 60.0
_DEFAULT_LIMIT = 50
_MAX_PROMPT_CHARS = 12_000


async def fetch_thread(
    client: Any,
    channel: str,
    thread_ts: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    ttl: float = _DEFAULT_TTL,
) -> list[dict]:
    """Return the ordered list of messages in a Slack thread.

    Each message dict has at least {user, text, ts, thread_ts?}. The root
    message is element 0. Returns [] on error (logged; never raises).
    """
    key = (channel, thread_ts)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    try:
        resp = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"conversations.replies failed for {channel}/{thread_ts}: {e}")
        return []

    if not resp.get("ok"):
        log.warning(
            f"conversations.replies not ok for {channel}/{thread_ts}: {resp.get('error')}"
        )
        return []

    messages = resp.get("messages", []) or []
    _CACHE[key] = (now, messages)
    return messages


def format_thread_for_prompt(
    messages: list[dict],
    *,
    user_names: dict[str, str] | None = None,
) -> str:
    """Render a thread as a chronological transcript for an agent prompt.

    `user_names` maps Slack user ids to display names. Unknown users show as
    their raw id.
    """
    user_names = user_names or {}
    lines: list[str] = []
    for m in messages:
        if m.get("subtype") in {"channel_join", "channel_leave"}:
            continue
        speaker = m.get("username") or user_names.get(m.get("user", ""), m.get("user", "?"))
        if m.get("bot_id") and not m.get("username"):
            speaker = "brain"
        text = (m.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{speaker}]: {text}")
    transcript = "\n".join(lines)
    if len(transcript) <= _MAX_PROMPT_CHARS:
        return transcript
    return "[earlier thread text truncated]\n" + transcript[-_MAX_PROMPT_CHARS:]
